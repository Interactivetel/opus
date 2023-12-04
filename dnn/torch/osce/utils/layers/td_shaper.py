import torch
from torch import nn
import torch.nn.functional as F

from utils.complexity import _conv1d_flop_count

class TDShaper(nn.Module):
    COUNTER = 1

    def __init__(self,
                 feature_dim,
                 frame_size=160,
                 avg_pool_k=4,
                 innovate=False,
                 pool_after=False,
                 activation='leaky_relu',
                 pre_scale=False
    ):
        """

        Parameters:
        -----------


        feature_dim : int
            dimension of input features

        frame_size : int
            frame size

        avg_pool_k : int, optional
            kernel size and stride for avg pooling

        padding : List[int, int]

        """

        super().__init__()


        self.feature_dim    = feature_dim
        self.frame_size     = frame_size
        self.avg_pool_k     = avg_pool_k
        self.innovate       = innovate
        self.pool_after     = pool_after
        self.pre_scale      = pre_scale

        assert frame_size % avg_pool_k == 0
        self.env_dim = frame_size // avg_pool_k + 1

        if activation == 'leaky_relu':
            self.activation = torch.nn.LeakyReLU(0.2)
        elif activation == 'tanh':
            self.activation = torch.tanh
        else:
            raise ValueError(f'invalid activation {activation}')

        # feature transform
        if pre_scale:
            pre_scale_out_channels = (self.env_dim + 3) // 4
            self.tenv_pre_scale = nn.Linear(self.env_dim, pre_scale_out_channels, 1)
            self.feature_alpha1 = nn.Conv1d(self.feature_dim + pre_scale_out_channels, frame_size, 2)
        else:
            self.feature_alpha1 = nn.Conv1d(self.feature_dim + self.env_dim, frame_size, 2)
        self.feature_alpha2 = nn.Conv1d(frame_size, frame_size, 2)

        if self.innovate:
            self.feature_alpha1b = nn.Conv1d(self.feature_dim + self.env_dim, frame_size, 2)
            self.feature_alpha1c = nn.Conv1d(self.feature_dim + self.env_dim, frame_size, 2)

            self.feature_alpha2b = nn.Conv1d(frame_size, frame_size, 2)
            self.feature_alpha2c = nn.Conv1d(frame_size, frame_size, 2)


    def flop_count(self, rate):

        frame_rate = rate / self.frame_size

        shape_flops = sum([_conv1d_flop_count(x, frame_rate) for x in (self.feature_alpha1, self.feature_alpha2)]) + 11 * frame_rate * self.frame_size

        if self.innovate:
            inno_flops = sum([_conv1d_flop_count(x, frame_rate) for x in (self.feature_alpha1b, self.feature_alpha2b, self.feature_alpha1c, self.feature_alpha2c)]) + 22 * frame_rate * self.frame_size
        else:
            inno_flops = 0

        return shape_flops + inno_flops

    def envelope_transform(self, x):

        x = torch.abs(x)
        if self.pool_after:
            x = torch.log(x + .5**16)
            x = F.avg_pool1d(x, self.avg_pool_k, self.avg_pool_k)
        else:
            x = F.avg_pool1d(x, self.avg_pool_k, self.avg_pool_k)
            x = torch.log(x + .5**16)

        x = x.reshape(x.size(0), -1, self.env_dim - 1)
        avg_x = torch.mean(x, -1, keepdim=True)

        x = torch.cat((x - avg_x, avg_x), dim=-1)

        if self.pre_scale:
            x = torch.tanh(self.tenv_pre_scale(x))

        return x

    def forward(self, x, features, debug=False):
        """ innovate signal parts with temporal shaping


        Parameters:
        -----------
        x : torch.tensor
            input signal of shape (batch_size, 1, num_samples)

        features : torch.tensor
            frame-wise features of shape (batch_size, num_frames, feature_dim)

        """

        batch_size = x.size(0)
        num_frames = features.size(1)
        num_samples = x.size(2)
        frame_size = self.frame_size

        # generate temporal envelope
        tenv = self.envelope_transform(x)

        # feature path
        f = torch.cat((features, tenv), dim=-1)
        f = F.pad(f.permute(0, 2, 1), [1, 0])
        alpha = self.activation(self.feature_alpha1(f))
        alpha = torch.exp(self.feature_alpha2(F.pad(alpha, [1, 0])))
        alpha = alpha.permute(0, 2, 1)

        if self.innovate:
            inno_alpha = F.leaky_relu(self.feature_alpha1b(f), 0.2)
            inno_alpha = torch.exp(self.feature_alpha2b(F.pad(inno_alpha, [1, 0])))
            inno_alpha = inno_alpha.permute(0, 2, 1)

            inno_x = F.leaky_relu(self.feature_alpha1c(f), 0.2)
            inno_x = torch.tanh(self.feature_alpha2c(F.pad(inno_x, [1, 0])))
            inno_x = inno_x.permute(0, 2, 1)

        # signal path
        y = x.reshape(batch_size, num_frames, -1)
        y = alpha * y

        if self.innovate:
            y = y + inno_alpha * inno_x

        return y.reshape(batch_size, 1, num_samples)
