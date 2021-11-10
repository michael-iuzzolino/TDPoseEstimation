import torch
from torch import nn
import utils.flops_benchmarker as flops_benchmark


class IdentityMapping(nn.Module):
    def __init__(self, in_channels, out_channels, mode="standard"):
        super(IdentityMapping, self).__init__()
        self._mode = mode
        self._setup_skip_conv(in_channels, out_channels)
        self._setup_alpha(out_channels)

    def _setup_skip_conv(self, in_channels, out_channels):
        self._use_skip_conv = in_channels != out_channels
        self.skip_conv = nn.Conv2d(
          in_channels=in_channels,
          out_channels=out_channels,
          kernel_size=1
        )

    def _setup_alpha(self, out_channels):
        if self._mode == "per_channel":
            alpha = torch.zeros((out_channels), requires_grad=True).float()
        elif self._mode == "single":
            alpha = torch.zeros((1), requires_grad=True).float()
        elif self._mode == "standard":
            alpha = torch.zeros((1), requires_grad=False).float()
        self.alpha = nn.Parameter(alpha)

    def _apply_gating(self, x):
        if self._mode == "per_channel":
            gated_identity = x * self.alpha[None, :, None, None]
        elif self._mode == "single":
            gated_identity = x * self.alpha
        elif self._mode == "standard":
            gated_identity = x
        return gated_identity

    def forward(self, x):
        if self._use_skip_conv:
            identity = self.skip_conv(x)
        else:
            identity = x

        # Gated identity
        gated_identity = self._apply_gating(identity)

        return gated_identity

        
class VanillaResblock(nn.Module):
    def __init__(self, in_channels, out_channels, **kwargs):
        super(VanillaResblock, self).__init__()

        self.conv1 = nn.Conv2d(
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=3,
            stride=1,
            padding=1,
        )
        self.conv2 = nn.Conv2d(
            in_channels=out_channels,
            out_channels=out_channels,
            kernel_size=3,
            stride=1,
            padding=1,
        )
        self.identity_mapping = IdentityMapping(
            in_channels=in_channels,
            out_channels=out_channels,
            mode=kwargs.get("identity_gating_mode", "standard"),
        )

        self._remap_output_dim = False
        if in_channels != out_channels:
            self._remap_output_dim = True
            self._remap_conv = nn.Conv2d(in_channels, out_channels, kernel_size=1)

        self.bn1 = nn.BatchNorm2d(out_channels)
        self.bn2 = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU()

    def forward(self, x):
        out = self.relu(self.bn1(self.conv1(x)))
        residual = self.bn2(self.conv2(out))
        identity = self.identity_mapping(x)

        # Identity + Residual
        out = residual + identity

        if self._remap_output_dim:
            out = self._remap_conv(out)

        return out


class MultiScaleResblock(nn.Module):
    def __init__(self, in_channels, out_channels, **kwargs):
        super(MultiScaleResblock, self).__init__()

        self.conv1 = nn.Conv2d(
            in_channels=in_channels,
            out_channels=in_channels // 2,
            kernel_size=3,
            stride=1,
            padding=1,
        )
        self.conv2 = nn.Conv2d(
            in_channels=in_channels // 2,
            out_channels=in_channels // 4,
            kernel_size=3,
            stride=1,
            padding=1,
        )
        self.conv3 = nn.Conv2d(
            in_channels=in_channels // 4,
            out_channels=in_channels // 4,
            kernel_size=3,
            stride=1,
            padding=1,
        )
        self.identity_mapping = IdentityMapping(
            in_channels=in_channels,
            out_channels=in_channels,
            mode=kwargs.get("identity_gating_mode", "standard"),
        )

        self._remap_output_dim = False
        if in_channels != out_channels:
            self._remap_output_dim = True
            self._remap_conv = nn.Conv2d(in_channels, out_channels, kernel_size=1)

        self.bn1 = nn.BatchNorm2d(in_channels // 2)
        self.bn2 = nn.BatchNorm2d(in_channels // 4)
        self.bn3 = nn.BatchNorm2d(in_channels // 4)
        self.relu = nn.ReLU()

    def forward(self, x):
        out1 = self.relu(self.bn1(self.conv1(x)))
        out2 = self.relu(self.bn2(self.conv2(out1)))
        out3 = self.relu(self.bn3(self.conv3(out2)))
        residual = torch.cat([out1, out2, out3], dim=1)
        identity = self.identity_mapping(x)

        # Identity + Residual
        out = residual + identity

        if self._remap_output_dim:
            out = self._remap_conv(out)

        return out


class HeadLayer(nn.Module):
    def __init__(self, in_channels, resblock, hidden_channels=64, kernel_size=7, **kwargs):
        super(HeadLayer, self).__init__()
        self.conv = nn.Conv2d(
            3,
            hidden_channels,
            kernel_size=kernel_size,
            stride=2,
            padding=(kernel_size - 1) // 2,
        )

        self.bn = nn.BatchNorm2d(hidden_channels)
        self.relu = nn.ReLU()

        self.pool = nn.MaxPool2d((2,2))
        self.res_1 = resblock(
            in_channels=hidden_channels,
            out_channels=hidden_channels,
            **kwargs,
        )
        self.res_2 = resblock(
            in_channels=hidden_channels,
            out_channels=hidden_channels,
            **kwargs,
        )
        self.res_3 = resblock(
            in_channels=hidden_channels,
            out_channels=in_channels,
            **kwargs,
        )

    def forward(self, x):
        out = self.relu(self.bn(self.conv(x)))
        out = self.res_1(out)
        out = self.pool(out)
        out = self.res_2(out)
        out = self.res_3(out)
        return out


class MergeDecoder(nn.Module):
    def __init__(self, mode, in_channels, out_channels, **kwargs):
        super(MergeDecoder, self).__init__()
        self._merge_mode = mode

        self.up = nn.Upsample(scale_factor=2, mode="nearest")
        if self._merge_mode == "addition":
            in_channels //= 2
        self.decode_conv = nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size=3,
            stride=1,
            padding=1,
        )
        self.bn = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU()

    def forward(self, x, down_feature):
        residual = self.up(x)

        if self._merge_mode == "addition":
            out = residual + down_feature
        elif self._merge_mode == "concat":
            out = torch.cat([residual, down_feature], dim=1)

        out = self.relu(self.bn(self.decode_conv(out)))
        return out


class HourGlass(nn.Module):
    def __init__(
        self,
        resblock,
        stack_i,
        in_channels,
        hidden_channels=None,
        n_joints=16,
        merge_mode="addition",
        **kwargs,
    ):
        super(HourGlass, self).__init__()
        self._stack_i = stack_i
        self._merge_mode = merge_mode
        self.relu = nn.ReLU()

        # Pooling and upsampling ops
        self.pool = nn.MaxPool2d((2, 2))
        self.up = nn.Upsample(scale_factor=2, mode="nearest")

        self.use_conversion_conv = False
        if hidden_channels and in_channels != hidden_channels:
            self.use_conversion_conv = True
            self.conversion_in_conv = nn.Conv2d(
                in_channels=in_channels,
                out_channels=hidden_channels,
                kernel_size=1,
            )
            self.conversion_out_conv = nn.Conv2d(
                in_channels=hidden_channels,
                out_channels=in_channels,
                kernel_size=1,
            )

            in_channels = hidden_channels

        # Encoder
        self.encode_1 = nn.Sequential(
            resblock(
                in_channels=in_channels,
                out_channels=in_channels,
                **kwargs
            ),
            nn.MaxPool2d((2,2)),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, stride=1, padding=1),
        )

        self.encode_2 = nn.Sequential(
            resblock(
                in_channels=in_channels,
                out_channels=in_channels,
                **kwargs
            ),
            nn.MaxPool2d((2,2)),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, stride=1, padding=1),
        )

        self.encode_3 = nn.Sequential(
            resblock(
                in_channels=in_channels,
                out_channels=in_channels,
                **kwargs
            ),
            nn.MaxPool2d((2,2)),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, stride=1, padding=1),
        )

        self.encode_4 = nn.Sequential(
            resblock(
                in_channels=in_channels,
                out_channels=in_channels,
                **kwargs
            ),
            nn.MaxPool2d((2,2)),
            nn.Conv2d(in_channels, in_channels, kernel_size=3, stride=1, padding=1),
        )

        # Decoder
        self.decode_1 = MergeDecoder(
            mode=self._merge_mode,
            in_channels=in_channels * 2,
            out_channels=in_channels,
            **kwargs,
        )
        self.decode_2 = MergeDecoder(
            mode=self._merge_mode,
            in_channels=in_channels * 2,
            out_channels=in_channels,
            **kwargs,
        )
        self.decode_3 = MergeDecoder(
            mode=self._merge_mode,
            in_channels=in_channels * 2,
            out_channels=in_channels,
            **kwargs,
        )

        self.final_up = nn.Conv2d(
            in_channels,
            in_channels,
            kernel_size=3,
            stride=1,
            padding=1,
        )

        # Feature map
        self.feature_map = nn.Sequential(
          resblock(
              in_channels=in_channels,
              out_channels=in_channels,
              **kwargs,
          ),
          nn.Conv2d(in_channels, in_channels, kernel_size=1, stride=1),
          nn.BatchNorm2d(in_channels),
          self.relu,
        )

        # Logit map
        self.logit_map = nn.Sequential(
          nn.Conv2d(in_channels, n_joints, kernel_size=1, stride=1)
        )

        # Remaps
        self.remap = nn.Sequential(
          nn.Conv2d(n_joints, in_channels, kernel_size=1, stride=1),
          self.relu,
        )

    def forward(self, x):
        if self.use_conversion_conv:
            x = self.conversion_in_conv(x)

        # Encoder
        down1 = self.encode_1(x)
        down2 = self.encode_2(down1)
        down3 = self.encode_3(down2)
        down4 = self.encode_4(down3)

        # Decoder
        up1 = self.decode_1(down4, down3)
        up2 = self.decode_2(up1, down2)
        up3 = self.decode_3(up2, down1)

        up4 = self.up(up3)
        out = self.final_up(up4)

        if self.use_conversion_conv:
            out = self.conversion_out_conv(out)

        features = self.feature_map(out)
        logits = self.logit_map(features)
        remap_i = self.remap(logits)
        residual = features + remap_i

        return residual, logits


class PoseNet(nn.Module):
    def __init__(
      self,
      resblock,
      n_stacks=1,
      n_features=128,
      n_joints=16,
      merge_mode="addition",
      **kwargs,
    ):
        super(PoseNet, self).__init__()
        self._n_stacks = n_stacks
        self._n_features = n_features
        self._n_joints = n_joints
        self._merge_mode = merge_mode
        self._kwargs = kwargs
        self.relu = nn.ReLU()

        # Head layer
        self.head_layer = HeadLayer(
            in_channels=n_features,
            resblock=resblock,
            hidden_channels=64,
            **kwargs,
        )

        # Setup hourglasses
        self._setup_hgs(resblock)

    def _setup_hgs(self, resblock):
        if self._kwargs.get("share_weights", False):
            hg_model = HourGlass(
                resblock=resblock,
                stack_i=0,
                in_channels=self._n_features,
                merge_mode=self._merge_mode,
                n_joints=self._n_joints,
                **self._kwargs
            )
            self.hgs = nn.ModuleList([hg_model for i in range(self._n_stacks)])
        else:
            self.hgs = nn.ModuleList([
                HourGlass(
                    resblock=resblock,
                    stack_i=i,
                    in_channels=self._n_features,
                    merge_mode=self._merge_mode,
                    n_joints=self._n_joints,
                    **self._kwargs
                )
              for i in range(self._n_stacks)
            ])

    def forward(self, x, log_flops=False):
        x = self.head_layer(x)
        logits = []
        total_flops = []
        for stack_i in range(self._n_stacks):
            if log_flops:
                flops_benchmark.init(self)
            identity = x.clone()
            residual, logit_i = self.hgs[stack_i](x)
            logits.append(logit_i)
            x = identity + residual

            if log_flops:
                n_flops = self.compute_total_flops()
                total_flops.append(n_flops)

        logits = torch.stack(logits)
        if log_flops:
            self.total_flops = total_flops
        return logits


def get_pose_net(cfg):
    n_hg_stacks = cfg.MODEL.EXTRA.N_HG_STACKS
    if "SHARE_HG_WEIGHTS" in cfg.MODEL.EXTRA:
        share_weights = cfg.MODEL.EXTRA.SHARE_HG_WEIGHTS
    else:
        share_weights = False
    
    if cfg.MODEL.EXTRA.RESBLOCK_TYPE == "multiscale":
        resblock = MultiScaleResblock
    else:
        resblock = VanillaResblock

    model = PoseNet(
        resblock=resblock,
        n_stacks=n_hg_stacks,
        n_features=cfg.MODEL.NUM_CHANNELS,
        n_joints=cfg.MODEL.NUM_JOINTS,
        merge_mode=cfg.MODEL.MERGE_MODE,
        identity_gating_mode=cfg.MODEL.IDENTITY_GATING_MODE,
        share_weights=share_weights,
    )

    return model
