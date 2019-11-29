"""
Push Sum Gossip class for distributed averaging using column stochastic mixing

:author: Mido Assran
:description: Distributed averaging using column stochastic mixing.
              Based on the paper (kempe2003Gossip-based)
"""

import time
from collections import defaultdict

from mpi4py import MPI
import numpy as np

from .gossip_comm import GossipComm

# Message passing and network variables
COMM = GossipComm.comm
SIZE = GossipComm.size
UID = GossipComm.uid
NAME = GossipComm.name


class PushSumAverager(object):
    """
    Distributed column stochastic averaging.

    :param peers: UniqueIDs of neighbouring peers in net. (used for comm.)
    :param in_degree: Num. messages to expect at each itr.
                      (only use in static synchronous nets.)
    """

    def __init__(self, peers=[(UID+1) % SIZE], in_degree=SIZE):
        """ Initialize the distributed averaging settings """

        # # Break on all numpy warnings
        # np.seterr(all='raise')

        self.peers = peers
        self.out_degree = len(self.peers)
        self.in_degree = in_degree

        self.info_list = []
        self.out_reqs = defaultdict(list)

    def make_stochastic_weight_column(self):
        """ Creates a column of weights for the mixing matrix. """
        column = {}
        lo_p = 1.0 / (self.out_degree + 1.0)
        out_p = [1.0 / (self.out_degree + 1.0) for _ in range(self.out_degree)]
        # lo_p = 0.9
        # out_p = [0.1 / self.out_degree for _ in range(self.out_degree)]
        column['lo_p'] = lo_p
        column['out_p'] = out_p
        return column

    def push_messages_to_peers(self, peers, consensus_column, ps_w, ps_n):
        """
        Send scaled push sum numerator and push sum weights to peers.

        :type peers: list[int]
        :type consensus_column: list[float]
        :type ps_w: float
        :type ps_n: float
        :rtype: void
        """
        print('%s: sending message to peers' % UID)
        for cc_w, peer_uid in zip(consensus_column, peers):
            # -- send message to peer
            push_message = np.append(ps_n * cc_w, ps_w * cc_w)
            req = COMM.Ibsend(push_message, dest=peer_uid, tag=1352)
            self.out_reqs[peer_uid].append(req)

    def recieve_asynchronously(self, gossip_value):
        """
        Probe buffer (non-blocking) & and retrieve all messages until the
        receive buffer is empty.

        :rtype: dict('num_rcvd': int, 'ps_w': float,
                     'ps_n': np.array[float] or float)
        """

        info = MPI.Status()
        ps_n = np.zeros(gossip_value.size, dtype=np.float64)
        ps_w = 0.
        num_rcvd = 0
        while COMM.Iprobe(source=MPI.ANY_SOURCE, status=info, tag=1352):
            self.info_list.append(info)
            print('%s: receiving message from %s' % (UID, info.source))
            data = np.empty(gossip_value.size + 1, dtype=np.float64)
            COMM.Recv(data, info.source)
            ps_n += data[:-1]
            ps_w += data[-1]
            num_rcvd += 1
            info = MPI.Status()
        self.info_list.clear()

        return {'num_rcvd': num_rcvd, 'ps_w': ps_w, 'ps_n': ps_n}

    def receive_synchronously(self, gossip_value):
        """
        Probe buffer (blocking) & retrieve all expected messages.

        :rtype: dict('num_rcvd': int, 'ps_w': float,
                     'ps_n': np.array[float] or float)
        """

        ps_n = np.zeros(gossip_value.size, dtype=np.float64)
        ps_w = 0
        num_rcvd = 0
        for _ in range(self.in_degree):
            info = MPI.Status()
            # -- timeout to avoid deadlocks
            start_time = time.time()
            break_flag = False
            while not COMM.Iprobe(source=MPI.ANY_SOURCE, status=info,
                                  tag=1352):
                if time.time() > start_time + 10:
                    break_flag = True
                    break
            if break_flag:
                break
            # -- receive message
            print('%s: receiving message from %s' % (UID, info.source))
            data = np.empty(gossip_value.size + 1, dtype=np.float64)
            COMM.Recv(data, info.source, tag=1352)
            num_rcvd += 1
            ps_n += data[:-1]
            ps_w += data[-1]

        return {'num_rcvd': num_rcvd, 'ps_w': ps_w, 'ps_n': ps_n}

    def gossip(self, ps_numerator, ps_weight=1.0, just_probe=False,
               asynch=True):
        """
        Gossip averaging

        :type gossip_value: float
        :type ps_weight: float
        :type just_probe: Boolean
        :rtype:
               log is False: dict('avg': float,
                                  'ps_n': float,
                                  'ps_w': float,
                                  'rcvd_flag': Boolean)
        """

        # -- initialize push sum gossip
        ps_n = np.array(ps_numerator, dtype=np.float64)  # push sum numerator
        ps_w = np.array(ps_weight, dtype=np.float64)  # push sum weight
        avg = ps_n / ps_w  # push sum estimate

        # -- check if last messages to peers were sent
        for peer_uid in self.peers:
            done_indices = []
            for i, req in enumerate(self.out_reqs[peer_uid]):
                if not req.test()[0]:
                    # -- not done sending msgs, just-probe if asynch
                    just_probe = asynch
                    continue
                done_indices.append(i)
            for index in sorted(done_indices, reverse=True):
                    del self.out_reqs[peer_uid][index]

        # -- push-messages to peers
        if not just_probe:
            column = self.make_stochastic_weight_column()
            out_p = column['out_p']  # outgoing mixing weights (vector)
            lo_p = column['lo_p']  # loop-back mixing weight (scalar)
            self.push_messages_to_peers(self.peers, out_p, ps_w, ps_n)
            ps_n *= lo_p
            ps_w *= lo_p
        else:
            print('%s: just probing' % UID)

        if asynch:
            rcvd = self.recieve_asynchronously(ps_n)
        else:
            rcvd = self.receive_synchronously(ps_n)
        ps_n += rcvd['ps_n'].reshape(ps_n.shape)
        ps_w += rcvd['ps_w']
        num_rcvd = rcvd['num_rcvd']
        avg = ps_n / ps_w
        print('%s: received %s messages' % (UID, num_rcvd))

        return {'avg': avg,
                'ps_w': ps_w,
                'ps_n': ps_n,
                'num_rcvd': num_rcvd,
                'just_probed': just_probe}


if __name__ == "__main__":

    def demo(gossip_value):
        """
        Demo of the use of the PushSumGossipAverager class.

        To run the demo, run the following form the command line:
            mpiexec -n $(num_nodes) python -m maopy.push_sum_gossip
        """

        # Initialize averager
        psga = PushSumAverager(peers=[(UID + 1) % SIZE, (UID + 2) % SIZE],
                               in_degree=2,
                               asynch=True)

        ps_n = gossip_value
        ps_w = 1.
        for _ in range(100):
            rcvd = psga.gossip(ps_n, ps_w)
            ps_n = rcvd['ps_n']
            ps_w = rcvd['ps_w']

        print('%s: (%s) ps_w:%s' % (UID, rcvd['avg'], rcvd['ps_w']))

    # Run a demo where nodes average their unique IDs
    demo(gossip_value=UID)
