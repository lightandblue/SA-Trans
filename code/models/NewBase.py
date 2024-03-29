import torch
from torch import nn
from torch.nn import functional as F
from torchvision.models import resnet50
from models.i3res import I3ResNet
from models.ResNet import ResNet_rgb
import copy
import inspect
from functools import reduce
import cv2
from torch.nn import BatchNorm2d as bn

resnet_2D = resnet50(pretrained=True)
resnet_3D = resnet50(pretrained=True)


def conv3x3(in_planes, out_planes, stride=1):
    "3x3 convolution with padding"
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=1, bias=False)
class TransBasicBlock(nn.Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, upsample=None, **kwargs):
        super(TransBasicBlock, self).__init__()
        self.conv1 = conv3x3(inplanes, inplanes)
        self.bn1 = nn.BatchNorm2d(inplanes)
        self.relu = nn.ReLU(inplace=True)
        if upsample is not None and stride != 1:
            self.conv2 = nn.ConvTranspose2d(inplanes, planes,
                                            kernel_size=3, stride=stride, padding=1,
                                            output_padding=1, bias=False)
        else:
            self.conv2 = conv3x3(inplanes, planes, stride)
        self.bn2 = nn.BatchNorm2d(planes)
        self.upsample = upsample
        self.stride = stride

    def forward(self, x):
        residual = x

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)

        if self.upsample is not None:
            residual = self.upsample(x)

        out += residual
        out = self.relu(out)

        return out


class ChannelAttention(nn.Module):
    def __init__(self, in_planes, ratio=16):
        super(ChannelAttention, self).__init__()

        self.max_pool = nn.AdaptiveMaxPool2d(1)

        self.fc1 = nn.Conv2d(in_planes, in_planes // 16, 1, bias=False)
        self.relu1 = nn.ReLU()
        self.fc2 = nn.Conv2d(in_planes // 16, in_planes, 1, bias=False)

        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        max_out = self.fc2(self.relu1(self.fc1(self.max_pool(x))))
        out = max_out
        return self.sigmoid(out)


class ChannelAttention_3D(nn.Module):
    def __init__(self, in_planes, ratio=16):
        super(ChannelAttention_3D, self).__init__()

        self.max_pool = nn.AdaptiveMaxPool3d(1)

        self.fc1 = nn.Conv3d(in_planes, in_planes // 16, 1, bias=False)
        self.relu1 = nn.ReLU()
        self.fc2 = nn.Conv3d(in_planes // 16, in_planes, 1, bias=False)

        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        max_out = self.fc2(self.relu1(self.fc1(self.max_pool(x))))
        out = max_out
        return self.sigmoid(out)


class AttenBlock(nn.Module):
    def __init__(self, channel=32):
        super(AttenBlock, self).__init__()
        self.atten_channel = ChannelAttention(channel)
        #self.atten_channel = eca_layer()

    def forward(self, x, y):
        attention = y.mul(self.atten_channel(x))
        out = y + attention

        return out


class AttenBlock_3D(nn.Module):
    def __init__(self, channel=32):
        super(AttenBlock_3D, self).__init__()
        self.atten_channel = ChannelAttention_3D(channel)
        #self.atten_channel = eca_layer()

    def forward(self, x, y):
        attention = y.mul(self.atten_channel(x))
        out = y + attention

        return out


class CoAttenBlock(nn.Module):
    def __init__(self, channel=32):
        super(CoAttenBlock, self).__init__()
        self.concaL = nn.Conv2d(in_channels=channel * 2, out_channels=channel, kernel_size=1)
        self.concaR = nn.Conv2d(in_channels=channel * 2, out_channels=channel, kernel_size=1)
        self.gateL = nn.Sequential(
            nn.Conv2d(in_channels=channel, out_channels=1, kernel_size=1), nn.Sigmoid(), )
        self.gateR = nn.Sequential(
            nn.Conv2d(in_channels=channel, out_channels=1, kernel_size=1), nn.Sigmoid(), )
        self.concaL_out = nn.Conv2d(in_channels=channel * 2, out_channels=channel, kernel_size=1)
        self.concaR_out = nn.Conv2d(in_channels=channel * 2, out_channels=channel, kernel_size=1)

    def forward(self, xlh, xll, xrh, xrl):
        bs, ch, hei, wei = xlh.size()

        # multi-level fusion
        xL = self.concaL(torch.cat([xlh, xll], dim=1))
        xR = self.concaR(torch.cat([xrh, xrl], dim=1))

        # cross attention fusion
        xL_reshape = torch.flatten(xL, start_dim=2, end_dim=3)
        xR_reshape = torch.flatten(xR, start_dim=2, end_dim=3)

        Affinity = torch.matmul(xL_reshape.permute(0, 2, 1), xR_reshape)
        AffinityAtten1 = F.softmax(Affinity, dim=1)
        AffinityAtten2 = F.softmax(Affinity, dim=2)
        #debug = torch.sum(AffinityAtten2[0,0,:])
        AffinityBranch1 = torch.matmul(xL_reshape, AffinityAtten1)
        AffinityBranch1 = AffinityBranch1.reshape([bs, ch, hei, wei])
        AffinityBranch1_gate = self.gateL(AffinityBranch1)
        AffinityBranch1 = AffinityBranch1.mul(AffinityBranch1_gate)
        AffinityBranch2 = torch.matmul(xR_reshape, AffinityAtten2)
        AffinityBranch2 = AffinityBranch2.reshape([bs, ch, hei, wei])
        AffinityBranch2_gate = self.gateR(AffinityBranch2)
        AffinityBranch2 = AffinityBranch2.mul(AffinityBranch2_gate)

        out_L = self.concaL_out(torch.cat([xL, AffinityBranch1], dim=1))
        out_R = self.concaR_out(torch.cat([xR, AffinityBranch2], dim=1))

        return out_L, out_R


class CoAttenBlock_multi(nn.Module):
    def __init__(self):
        super(CoAttenBlock_multi, self).__init__()
        self.CoAtten = CoAttenBlock()

    def forward(self, xlh, xll, xrh, xrl):
        T_dimension = xrh.size()[2]

        out_L, out_R = [], []
        for tt in range(T_dimension):
            curr_l, curr_r = self.CoAtten(xlh, xll, xrh[:, :, tt, :, :], xrl[:, :, tt, :, :])
            out_L.append(curr_l.unsqueeze(2))
            out_R.append(curr_r.unsqueeze(2))
        out_L = torch.cat(out_L, dim=2)
        out_R = torch.cat(out_R, dim=2)

        out_L = torch.mean(out_L, dim=2)

        return out_L, out_R


class BasicResConv2d(nn.Module):
    def __init__(self, in_planes, out_planes, kernel_size, stride=1, padding=0, dilation=1):
        super(BasicResConv2d, self).__init__()
        self.conv = nn.Conv2d(in_planes, out_planes,
                              kernel_size=kernel_size, stride=stride,
                              padding=padding, dilation=dilation, bias=False)
        self.bn = nn.BatchNorm2d(out_planes)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        residual = x
        x = self.conv(x)
        x = self.bn(x)
        out = x + residual
       # out = self.relu(out)

        return out


class BasicResConv3d(nn.Module):
    def __init__(self, in_planes, out_planes, kernel_size, stride=1, padding=0, dilation=1):
        super(BasicResConv3d, self).__init__()
        self.conv = nn.Conv3d(in_planes, out_planes,
                              kernel_size=kernel_size, stride=stride,
                              padding=padding, dilation=dilation, bias=False)
        self.bn = nn.BatchNorm3d(out_planes)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        residual = x
        x = self.conv(x)
        x = self.bn(x)
        out = x + residual
       # out = self.relu(out)

        return out


class SpatialAttention(nn.Module):
    def __init__(self, kernel_size=7):
        super(SpatialAttention, self).__init__()

        assert kernel_size in (3, 7), 'kernel size must be 3 or 7'
        padding = 3 if kernel_size == 7 else 1

        self.conv1 = nn.Conv2d(1, 1, kernel_size, padding=padding, bias=False)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        max_out, _ = torch.max(x, dim=1, keepdim=True)
        x=max_out
        x = self.conv1(x)
        return self.sigmoid(x)


class aggregation_3D(nn.Module):
    def __init__(self, channel):
        super(aggregation_3D, self).__init__()
        self.upsample2 = nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True)
        self.upsample2_3D = nn.Upsample(scale_factor=2, mode='trilinear', align_corners=True)
        self.upsample8 = nn.Upsample(scale_factor=8, mode='bilinear', align_corners=True)

        # synergistic attention
        self.RB_L1 = BasicResConv2d(channel, channel, 3, padding=1)
        self.CA_L1 = AttenBlock()
        self.RB_R1 = BasicResConv3d(channel, channel, 3, padding=1)
        self.CA_R1 = AttenBlock_3D()
        self.CoA_1 = CoAttenBlock_multi()

        self.RB_L2 = BasicResConv2d(channel, channel, 3, padding=1)
        self.CA_L2 = AttenBlock()
        self.RB_R2 = BasicResConv3d(channel, channel, 3, padding=1)
        self.CA_R2 = AttenBlock_3D()
        self.CoA_2 = CoAttenBlock_multi()

        # multi-supervision
        self.out_conv_L = nn.Conv2d(32 * 1, 1, kernel_size=1, stride=1, bias=True)
        self.out_conv_R = nn.Conv2d(32 * 1, 1, kernel_size=1, stride=1, bias=True)

        # AA fusion
        self.FSAtten = ChannelAttention_3D(channel)
        self.FSCollapse = nn.Conv3d(in_channels=32, out_channels=32, kernel_size=(8, 1, 1))
        self.CARefine = ChannelAttention(channel)
        self.SARefine = SpatialAttention()

        # Components of PTM module
        self.inplanes = 32
        self.deconv1 = self._make_transpose(TransBasicBlock, 32, 3, stride=2)
        self.deconv2 = self._make_transpose(TransBasicBlock, 32, 3, stride=2)
        self.deconv3 = self._make_transpose(TransBasicBlock, 32, 3, stride=2)
        self.agant1 = self._make_agant_layer(32 * 2, 32)
        self.agant2 = self._make_agant_layer(32, 32)
        self.agant3 = self._make_agant_layer(32, 32)
        self.out_conv = nn.Conv2d(32, 1, kernel_size=1, stride=1, bias=True)

    def forward(self, l1, l2, l3, r1, r2, r3):
        # SA1
        add_1_in_l1 = self.RB_L1(self.upsample2(l1))
        add_1_in_l2 = self.CA_L1(l1, l2)
        add_1_in_R1 = self.RB_R1(self.upsample2_3D(r1))
        add_1_in_R2 = self.CA_R1(r1, r2)
        add_1_out_L, add_1_out_R = self.CoA_1(add_1_in_l1, add_1_in_l2, add_1_in_R1, add_1_in_R2)

        # SA2
        add_1_out_L = self.RB_L2(self.upsample2(add_1_out_L))
        add_1_out_R = self.RB_R2(self.upsample2_3D(add_1_out_R))
        add_2_in_L = self.CA_L2(l2, l3)
        add_2_in_R = self.CA_R2(r2, r3)
        add_2_out_L, add_2_out_R = self.CoA_2(add_1_out_L, add_2_in_L, add_1_out_R, add_2_in_R)

        # AA
        add_2_out_R = add_2_out_R.mul(self.FSAtten(add_2_out_R))
        add_2_out_R = self.FSCollapse(add_2_out_R).squeeze(2)

        temp = add_2_out_L.mul(self.CARefine(add_2_out_L))
        temp = temp.mul(self.SARefine(temp))
        add_2_out_R = add_2_out_R + temp

        # Multi supervision
        out_L = self.upsample8(self.out_conv_L(add_2_out_L))
        out_R = self.upsample8(self.out_conv_R(add_2_out_R))
        add_2_out = torch.cat([add_2_out_L, add_2_out_R], dim=1)

        out = self.agant1(add_2_out)
        out = self.deconv1(out)
        out = self.agant2(out)
        out = self.deconv2(out)
        out = self.agant3(out)
        out = self.deconv3(out)
        out = self.out_conv(out)

        return out, out_L, out_R

    def _make_agant_layer(self, inplanes, planes):
        layers = nn.Sequential(
            nn.Conv2d(inplanes, planes, kernel_size=1,
                      stride=1, padding=0, bias=False),
            nn.BatchNorm2d(planes),
            nn.ReLU(inplace=True)
        )
        return layers

    def _make_transpose(self, block, planes, blocks, stride=1):
        upsample = None
        if stride != 1:
            upsample = nn.Sequential(
                nn.ConvTranspose2d(self.inplanes, planes,
                                   kernel_size=2, stride=stride,
                                   padding=0, bias=False),
                nn.BatchNorm2d(planes),
            )
        elif self.inplanes != planes:
            upsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(planes),
            )

        layers = []
        for i in range(1, blocks):
            layers.append(block(self.inplanes, self.inplanes))
        layers.append(block(self.inplanes, planes, stride, upsample))
        self.inplanes = planes

        return nn.Sequential(*layers)


class BasicConv2d(nn.Module):
    def __init__(self, in_planes, out_planes, kernel_size, stride=1, padding=0, dilation=1):
        super(BasicConv2d, self).__init__()
        self.conv = nn.Conv2d(in_planes, out_planes,
                              kernel_size=kernel_size, stride=stride,
                              padding=padding, dilation=dilation, bias=False)
        self.bn = nn.BatchNorm2d(out_planes)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        return x


class BasicConv3d(nn.Module):
    def __init__(self, in_planes, out_planes, kernel_size, stride=1, padding=0, dilation=1):
        super(BasicConv3d, self).__init__()
        self.conv = nn.Conv3d(in_planes, out_planes,
                              kernel_size=kernel_size, stride=stride,
                              padding=padding, dilation=dilation, bias=False)
        self.bn = nn.BatchNorm3d(out_planes)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        return x


class RFB(nn.Module):
    # RFB-like multi-scale module
    def __init__(self, in_channel, out_channel):
        super(RFB, self).__init__()
        self.relu = nn.ReLU(True)
        self.branch0 = nn.Sequential(
            BasicConv2d(in_channel, out_channel, 1),
        )
        self.branch1 = nn.Sequential(
            BasicConv2d(in_channel, out_channel, 1),
            BasicConv2d(out_channel, out_channel, kernel_size=(1, 3), padding=(0, 1)),
            BasicConv2d(out_channel, out_channel, kernel_size=(3, 1), padding=(1, 0)),
            BasicConv2d(out_channel, out_channel, 3, padding=3, dilation=3)
        )
        self.branch2 = nn.Sequential(
            BasicConv2d(in_channel, out_channel, 1),
            BasicConv2d(out_channel, out_channel, kernel_size=(1, 5), padding=(0, 2)),
            BasicConv2d(out_channel, out_channel, kernel_size=(5, 1), padding=(2, 0)),
            BasicConv2d(out_channel, out_channel, 3, padding=5, dilation=5)
        )
        self.branch3 = nn.Sequential(
            BasicConv2d(in_channel, out_channel, 1),
            BasicConv2d(out_channel, out_channel, kernel_size=(1, 7), padding=(0, 3)),
            BasicConv2d(out_channel, out_channel, kernel_size=(7, 1), padding=(3, 0)),
            BasicConv2d(out_channel, out_channel, 3, padding=7, dilation=7)
        )
        self.conv_cat = BasicConv2d(4*out_channel, out_channel, 3, padding=1)
        self.conv_res = BasicConv2d(in_channel, out_channel, 1)

    def forward(self, x):
        x0 = self.branch0(x)
        x1 = self.branch1(x)
        x2 = self.branch2(x)
        x3 = self.branch3(x)

        x_cat = self.conv_cat(torch.cat((x0, x1, x2, x3), 1))

        x = self.relu(x_cat + self.conv_res(x))
        return x


class RFB_3D(nn.Module):
    # RFB-like multi-scale module
    def __init__(self, in_channel, out_channel):
        super(RFB_3D, self).__init__()
        self.relu = nn.ReLU(True)
        self.branch0 = nn.Sequential(
            BasicConv3d(in_channel, out_channel, 1),
        )
        self.branch1 = nn.Sequential(
            BasicConv3d(in_channel, out_channel, 1),
            BasicConv3d(out_channel, out_channel, kernel_size=(1, 1, 3), padding=(0, 0, 1)),
            BasicConv3d(out_channel, out_channel, kernel_size=(1, 3, 1), padding=(0, 1, 0)),
            BasicConv3d(out_channel, out_channel, 3, padding=3, dilation=3)
        )
        self.branch2 = nn.Sequential(
            BasicConv3d(in_channel, out_channel, 1),
            BasicConv3d(out_channel, out_channel, kernel_size=(1, 1, 5), padding=(0, 0, 2)),
            BasicConv3d(out_channel, out_channel, kernel_size=(1, 5, 1), padding=(0, 2, 0)),
            BasicConv3d(out_channel, out_channel, 3, padding=5, dilation=5)
        )
        self.branch3 = nn.Sequential(
            BasicConv3d(in_channel, out_channel, 1),
            BasicConv3d(out_channel, out_channel, kernel_size=(1, 1, 7), padding=(0, 0, 3)),
            BasicConv3d(out_channel, out_channel, kernel_size=(1, 7, 1), padding=(0, 3, 0)),
            BasicConv3d(out_channel, out_channel, 3, padding=7, dilation=7)
        )
        self.conv_cat = BasicConv3d(4*out_channel, out_channel, 3, padding=1)
        self.conv_res = BasicConv3d(in_channel, out_channel, 1)

    def forward(self, x):
        x0 = self.branch0(x)
        x1 = self.branch1(x)
        x2 = self.branch2(x)
        x3 = self.branch3(x)

        x_cat = self.conv_cat(torch.cat((x0, x1, x2, x3), 1))

        x = self.relu(x_cat + self.conv_res(x))
        return x


class SANetV2(nn.Module):
  def __init__(self):
    super(SANetV2, self).__init__()
    channel_decoder = 32

    # encoder
    self.encoder_rgb = ResNet_rgb()
    self.encoder_fs =I3ResNet(copy.deepcopy(resnet_3D))  # make sure the 3D ResNet loaded with ImageNet pretrain

    # decoder
    self.rfb_rgb_3 = RFB(512, channel_decoder)
    self.rfb_rgb_4 = RFB(1024, channel_decoder)
    self.rfb_rgb_5 = RFB(2048, channel_decoder)

    self.rfb_fs_3 = RFB_3D(512, channel_decoder)
    self.rfb_fs_4 = RFB_3D(1024, channel_decoder)
    self.rfb_fs_5 = RFB_3D(2048, channel_decoder)

    self.agg = aggregation_3D(channel_decoder)

    if self.training: self.initialize_weights()

  def forward(self, rgb, fss, depth):
    # guidance of high-level encoding of focus stacking
    E_rgb5, E_rgb4, E_rgb3, _, _ = self.encoder_rgb.forward(rgb)

    E_fs5, E_fs4, E_fs3, _, _ = self.encoder_fs.forward(fss)

    d3_rgb = self.rfb_rgb_3(E_rgb3)
    d4_rgb = self.rfb_rgb_4(E_rgb4)
    d5_rgb = self.rfb_rgb_5(E_rgb5)
    d3_fs = self.rfb_fs_3(E_fs3)
    d4_fs = self.rfb_fs_4(E_fs4)
    d5_fs = self.rfb_fs_5(E_fs5)

    pred_fuse, pred_L, pred_R = self.agg(d5_rgb, d4_rgb, d3_rgb, d5_fs, d4_fs, d3_fs)

    return pred_fuse, pred_L, pred_R

  def initialize_weights(self):
    pretrained_dict = resnet_2D.state_dict()
    all_params = {}
    for k, v in self.encoder_rgb.state_dict().items():
      if k in pretrained_dict.keys():
        v = pretrained_dict[k]
        all_params[k] = v
    self.encoder_rgb.load_state_dict(all_params)
