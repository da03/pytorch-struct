import torch
from .helpers import _Struct
import math
# from .sparse import *
import genbmm
# from pytorch_memlab import MemReporter
from .semirings import LogSemiring
from .semirings.fast_semirings import broadcast

Down, Mid, Up = 0, 1, 2
Open, Close = 0, 1


class Alignment(_Struct):
    def __init__(self, semiring=LogSemiring, sparse_rounds=3, local=False):
        self.semiring = semiring
        self.sparse_rounds = sparse_rounds
        self.local = local

    def _check_potentials(self, edge, lengths=None):
        batch, N_1, M_1, x = edge.shape
        assert x == 3
        if self.local:
            assert (edge[..., 0] <= 0).all(), "skips must be negative"
            assert (edge[..., 1] >= 0).all(), "alignment must be positive"
            assert (edge[..., 2] <= 0).all(), "skips must be negative"

        edge = self.semiring.convert(edge)

        N = N_1
        M = M_1
        if lengths is None:
            lengths = torch.LongTensor([N] * batch)

        assert max(lengths) <= N, "Length longer than edge scores"
        assert max(lengths) == N, "One length must be at least N"
        return edge, batch, N, M, lengths

    def _dp(self, log_potentials, lengths=None, force_grad=False):
        return self._dp_scan(log_potentials, lengths, force_grad)

    def _dp_scan(self, log_potentials, lengths=None, force_grad=False):
        "Compute forward pass by linear scan"
        # Setup
        semiring = self.semiring
        log_potentials.requires_grad_(True)
        ssize = semiring.size()
        log_potentials, batch, N, M, lengths = self._check_potentials(
            log_potentials, lengths
        )
        steps = N + M
        log_MN = int(math.ceil(math.log(steps, 2)))
        bin_MN = int(math.pow(2, log_MN))
        LOC = 2 if self.local else 1

        # Create a chart N, N, back
        charta = [None, None]
        chartb = [None]
        charta[0] = self._make_chart(
            1, (batch, bin_MN, 1, bin_MN,  2, 2, 3), log_potentials, force_grad
        )[0]
        charta[1] = self._make_chart(
            1, (batch, bin_MN // 2, 3, bin_MN, 2, 2, 3), log_potentials, force_grad
        )[0]

        # Init
        # This part is complicated. Rotate the scores by 45% and
        # then compress one.
        grid_x = torch.arange(N).view(N, 1).expand(N, M)
        grid_y = torch.arange(M).view(1, M).expand(N, M)
        rot_x = grid_x + grid_y
        rot_y = grid_y - grid_x + N

        # Ind
        ind = torch.arange(bin_MN)
        ind_M = ind
        ind_U = torch.arange(1, bin_MN)
        ind_D = torch.arange(bin_MN - 1)

        for b in range(lengths.shape[0]):
            point = (lengths[b] + M) // 2
            lim = point * 2

            charta[0][:, b, rot_x[:lim], 0, rot_y[:lim], :, :, :] = (
                log_potentials[:, b, :lim].unsqueeze(-2).unsqueeze(-2)
            )

            charta[1][:, b, point:, 1, ind, :, :, Mid] = semiring.one_(
                charta[1][:, b, point:, 1, ind, :, :, Mid]
            )

        for b in range(lengths.shape[0]):
            point = (lengths[b] + M) // 2
            lim = point * 2

            left_ = charta[0][:, b, 0:lim:2, 0]
            right = charta[0][:, b, 1:lim:2, 0]

            charta[1][:, b, :point, 1, ind_M] = torch.stack(
                [
                    left_[..., Down],
                    semiring.plus(
                        left_[..., Mid],
                        right[..., Mid],
                    ),
                    left_[..., Up],
                ],
                dim=-1,
            )

            y = torch.stack([ind_D, ind_U], dim=0)
            z = y.clone()
            z[0, :] = 2
            z[1, :] = 0

            charta[1][:, b, :point, z, y, :, :, :] = torch.stack(
                [
                    semiring.times(
                        left_[:, :,  ind_D, Open : Open + 1 :, :],
                        right[:, :,  ind_U, :, Open : Open + 1, Down : Down + 1],
                    ),
                    semiring.times(
                        left_[:, :,  ind_U, Open : Open + 1, :, :],
                        right[:, :,  ind_D, :, Open : Open + 1, Up : Up + 1],
                    ),
                ],
                dim=2,
            )


        chart = charta[1][..., :, :, :].permute(0, 1, 2, 5, 6, 7, 4, 3)

        # Scan
        def merge(x):
            inner = x.shape[-1]
            width = (inner -1) // 2
            left = (
                x[:, :, 0 : : 2, Open, :]
                .view(ssize, batch, -1, 1, 2, 3, bin_MN,  inner)
            )
            right = (
                x[:, :, 1 : : 2, :, Open]
                .view(ssize, batch, -1, 2, 1, 1, 3, bin_MN, inner)
            )

            st = []
            for op in (Mid,Up, Down):
                leftb, rightb, _ = broadcast(left, right[..., op,  :, :])
                leftb = genbmm.BandedMatrix(leftb, width, width, semiring.zero)
                rightb = genbmm.BandedMatrix(rightb, width, width, semiring.zero)
                leftb = leftb.transpose().col_shift(op-1).transpose()
                v = semiring.matmul(rightb, leftb).band_shift(op-1)
                v = v.data.view(ssize, batch, -1, 2, 2, 3, bin_MN, v.data.shape[-1])
                st.append(v)
                rsize = v.data.shape[-1]

            if self.local:
                def pad(v):
                    s = list(v.shape)
                    s[-1] = inner // 2
                    pads = torch.zeros(*s).fill_(semiring.zero)
                    return torch.cat([pads, v, pads], -1)
                left_ = (
                    x[:, :, 0 : : 2, Close, :]
                    .view(ssize, batch, -1, 1, 2, 3, bin_MN, inner)
                )
                left_ = pad(left)
                right = (
                    x[:, :, 1 : : 2, :, Close]
                    .view(ssize, batch, -1, 2, 1, 3, bin_MN, inner)
                )
                right = pad(right)

                st.append(torch.cat([semiring.zero_(left_.clone()), left_], dim=3))
                st.append(torch.cat([semiring.zero_(right.clone()), right], dim=4))
            return semiring.sum(torch.stack(st, dim=-1))

        for n in range(2, log_MN + 1):
            chart = merge(chart)

        if self.local:
            v = chart[..., 0, Close, Close, Mid, N, M - N + ((chart.shape[-1] -1)//2)]
        else:
            v = chart[..., 0, Open, Open, Mid, N, M - N + ((chart.shape[-1] -1)//2)]
        return v, [log_potentials], None

    @staticmethod
    def _rand(min_n=2):
        b = torch.randint(2, 4, (1,))
        N = torch.randint(min_n, 4, (1,))
        M = torch.randint(min_n, 4, (1,))
        return torch.rand(b, N, M, 3), (b.item(), (N).item())

    def enumerate(self, edge, lengths=None):
        semiring = self.semiring
        edge, batch, N, M, lengths = self._check_potentials(edge, lengths)
        d = {}
        d[0, 0] = [([(0, 0)], edge[:, :, 0, 0, 1])]
        # enum_lengths = torch.LongTensor(lengths.shape)
        for i in range(N):
            for j in range(M):
                d.setdefault((i + 1, j + 1), [])
                d.setdefault((i, j + 1), [])
                d.setdefault((i + 1, j), [])
                for chain, score in d[i, j]:
                    if i + 1 < N and j + 1 < M:
                        d[i + 1, j + 1].append(
                            (
                                chain + [(i + 1, j + 1)],
                                semiring.mul(score, edge[:, :, i + 1, j + 1, 1]),
                            )
                        )
                    if i + 1 < N:

                        d[i + 1, j].append(
                            (
                                chain + [(i + 1, j)],
                                semiring.mul(score, edge[:, :, i + 1, j, 2]),
                            )
                        )
                    if j + 1 < M:
                        d[i, j + 1].append(
                            (
                                chain + [(i, j + 1)],
                                semiring.mul(score, edge[:, :, i, j + 1, 0]),
                            )
                        )
        all_val = torch.stack([x[1] for x in d[N - 1, M - 1]], dim=-1)
        return semiring.unconvert(semiring.sum(all_val)), None
