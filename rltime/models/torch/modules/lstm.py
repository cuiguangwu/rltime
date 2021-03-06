import numpy as np
import torch

from .base import BaseModule
from ..utils import init_weight, repeat_interleave


class LSTM(BaseModule):
    """An LSTM module with experimental support for multi-sample handling of
    the state (i.e. in case of an IQN layer before the LSTM)"""

    def __init__(self, inp_shape, num_units, multi_sample_merge_mode="inner"):
        """Initialize an LSTM torch module

        Args:
            inp_shape: The input shape (Will be flattened to 1D in any case)
            num_units: Number of hidden units in the LSTM. The hidden-state
                size per timestep will be num_units*2*4B
            multi_sample_merge_mode: In case of a multi-sample batch (e.g.
                IQN), how to handle the LSTM input/output state (Which is
                single-sample)
                Options are:
                - 'outer': Repeat the state at the start of the sequence and
                  merge back (using mean) at the end
                - 'inner': repeat->merge the state on every timestep within the
                  sequence such that every timestep in the sequence has the
                  same input state for each sample (This allows the merge/mean
                  to participate in the backprop and may allow the model to
                  learn a more mean-friendly state representation)
                - Alternatively, the need for this option can be avoided
                  altogether by injecting the IQN layer after the LSTM layer
                Note this option is only relevant when there is an IQN layer
                before the LSTM layer, and only for training/bootstrapping
                (Acting does 1 timestep at a time therefore both options are
                equivalent)
        """
        super(LSTM, self).__init__()
        self.inp_size = np.prod(inp_shape)
        self.num_units = num_units
        assert(multi_sample_merge_mode in ['outer', 'inner'])
        self.multi_sample_merge_mode = multi_sample_merge_mode
        self.lstm_cell = torch.nn.LSTMCell(
            input_size=self.inp_size, hidden_size=num_units)
        self.out_shape = (num_units,)
        self.last_state = None

        init_weight(self.lstm_cell.weight_hh)
        init_weight(self.lstm_cell.weight_ih)

    def forward(self, x, hx, cx, initials, timesteps):
        # Flatten
        x = x.view(-1, self.inp_size)
        assert(hx.shape[1] == self.num_units)
        assert(cx.shape[1] == self.num_units)

        # Auto-detect a 'mult-sample' batch (e.g. IQN) from the batch size
        # compared to amount of hidden states
        assert(x.shape[0] % hx.shape[0] == 0)
        multi_sample = x.shape[0] // hx.shape[0]

        # The actual batch size in terms of sequence counts
        batch_size = x.shape[0] // timesteps

        # Take the initial state only from timestep0 of each batch item (The
        # mid-sequence states are generated by the LSTM in the loop below, if
        # timesteps>1)
        hx = hx.view(
            timesteps, batch_size // multi_sample, self.num_units)[0, ...]
        cx = cx.view(
            timesteps, batch_size // multi_sample, self.num_units)[0, ...]

        # Reshape the input by timestep
        x = x.view((timesteps, batch_size)+x.shape[1:])

        # Reshape the 'initials' by timestep, if it's a 'multi sample' we need
        # to repeat them accordingly
        assert(initials.shape == ((batch_size * timesteps) // multi_sample,))
        if multi_sample > 1:
            initials = repeat_interleave(initials, multi_sample, dim=0)
        initials = initials.view((timesteps, batch_size))

        out = []
        # Timestep loop
        for i, (x, initial) in enumerate(zip(x, initials)):
            if multi_sample > 1 and \
                    ((i == 0) or self.multi_sample_merge_mode == "inner"):
                # MultiSamples are assumed to be grouped together on the batch
                # axis so we need interleaved repeating of the state (And not
                # tiling)
                hx = repeat_interleave(hx, multi_sample, dim=0)
                cx = repeat_interleave(cx, multi_sample, dim=0)

            # Reset state to 0 on every 'initial' (i.e. on where a new episode
            # started mid-sequence)
            initial = initial.unsqueeze(-1)  # Broadcast to all state features
            hx = hx * (1 - initial)
            cx = cx * (1 - initial)

            # Run the LSTM cell for this timestep
            hx, cx = self.lstm_cell(x, (hx, cx))

            # Accumulate the outputs
            out.append(hx)

            if multi_sample > 1 and \
                    ((i == (timesteps - 1)) or
                        self.multi_sample_merge_mode == "inner"):
                # If it's a 'multi sample' batch (e.g. IQN), we need to reduce
                # back to a single sample state. Not clear what is correct here
                # as IQN paper doesn't do LSTM, but for now we just average the
                # state across the samples. In case of 'outer' merge mode, we
                # use the tiled state throughout the LSTM sequence and reduce
                # back only in the end, and in case of 'inner' merge mode we
                # merge/re-tile on every timestep
                hx = hx.view(-1, multi_sample, hx.shape[-1]).mean(1)
                cx = cx.view(-1, multi_sample, cx.shape[-1]).mean(1)
        out = torch.cat(out)

        # Save the current/last state to return in self.get_state()
        self.last_state = hx.data, cx.data

        return out

    def is_cuda(self):
        return next(self.lstm_cell.parameters()).is_cuda

    @staticmethod
    def is_recurrent():
        return True

    def get_state(self, initials):
        """Gets the state for next LSTM run.

        This includes the last output recurrent state, and the 'initials
        vector signifying it's an initial timestep in which case the state is
        zeroed (The initials are saved in the state, so that when rerunning the
        module at train time we know to reset the state mid-sequence. See
        forward()).
        These state values will be passed to forward() (In addition to 'x'
        itself and the 'timesteps')
        """
        initials = initials.astype("float32")
        batch_size = len(initials)
        if self.last_state is None:
            # If this is the 1st time we are called then all must be initial
            # states
            assert(np.all(initials))
            shape = (batch_size, self.num_units)
            self.last_state = torch.zeros(shape), torch.zeros(shape)
        hx, cx = self.last_state
        hx, cx = hx.cpu().numpy(), cx.cpu().numpy()
        mask = np.expand_dims(1 - initials, axis=-1)
        self.last_state = hx * mask, cx * mask

        assert(self.last_state[0].shape[0] == batch_size)
        assert(self.last_state[1].shape[0] == batch_size)
        return {
            "hx": self.last_state[0],
            "cx": self.last_state[1],
            "initials": initials
        }
