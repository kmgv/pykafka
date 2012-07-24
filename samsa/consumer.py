import logging
import itertools
import socket

from collections import namedtuple
from uuid import uuid4
from zookeeper import NoNodeException

from samsa.exceptions import ImproperlyConfigured
from samsa.utils.delayedconfig import DelayedConfiguration, requires_configuration

logger = logging.getLogger(__name__)

class PartitionName(namedtuple('Partition', ['broker_id', 'partition_id'])):

    @staticmethod
    def from_str(s):
        broker_id, partition_id = s.split('-')
        return PartitionName(broker_id, partition_id)

    def to_str(self):
        return "%s-%s" % (self.broker_id, self.partition_id)

    def __str__(self):
        return self.to_str()


class PartitionOwnerRegistry(object):
    """
    Manages the Partition Owner Registry for a particular Consumer.
    """

    def __init__(self, consumer, cluster, topic, group):
        self.consumer_id = consumer.id
        self.cluster = cluster
        self.path = '/consumers/%s/owners/%s' % (group, topic.name)
        self.cluster.zookeeper.ensure_path(self.path)

        self._partitions = set([])

        zk = self.cluster.zookeeper
        partitions = zk.get_children(self.path)

        for name in partitions:
            p = PartitionName.from_str(name)
            _, value = zk.get(self._path_from_partition(p))
            if value == self.consumer_id:
                self._partitions.add(p)

    def get(self):
        return self._partitions

    def remove(self, partitions):
        for p in partitions:
            assert p in self._partitions
            self.cluster.zookeeper.delete(self._path_from_partition(p))
            self._partitions.remove(p)

    def add(self, partitions):
        for p in partitions:
            self.cluster.zookeeper.create(
                self._path_from_partition(p), self.consumer_id, ephemeral=True
            )
            self._partitions.add(p)

    def _path_from_partition(self, p):
        return "%s/%s" % (self.path, p)


class Consumer(DelayedConfiguration):
    def __init__(self, cluster, topic, group):
        self.cluster = cluster
        self.topic = topic
        self.group = group
        self.id = "%s:%s" % (socket.gethostname(), uuid4())

        self.id_path = '/consumers/%s/ids' % self.group
        self.cluster.zookeeper.ensure_path(self.id_path)

        self.partition_owner_registry = PartitionOwnerRegistry(
            self, cluster, topic, group)

    def _configure(self, event=None):
        """
        Joins a consumer group and claims partitions.
        """

        path = '%s/%s' % (self.id_path, self.id)
        self.cluster.zookeeper.create(path, self.topic.name, ephemeral=True)

        self._rebalance()

    def _rebalance(self):
        logger.info('Rebalancing consumer %s for topic %s.' % (
            self.id, self.topic.name)
        )

        zk = self.cluster.zookeeper
        broker_path = '/brokers/ids'
        try:
            zk.get_children(broker_path, watch=self._rebalance)
        except NoNodeException:
            raise ImproperlyConfigured('The broker_path "%s" does not exist in your '
                'ZooKeeper cluster -- is your Kafka cluster running?' % broker_path)

        self.commit_offsets()

        # 3. all consumers in the same group as Ci that consume topic T
        consumer_ids = zk.get_children(self.id_path, watch=self._rebalance)
        participants = []
        for id_ in consumer_ids:
            topic, stat = zk.get("%s/%s" % (self.id_path, id_))
            if topic == self.topic.name:
                participants.append(id_)
        # 5.
        participants.sort()


        # 6.
        i = participants.index(self.id)
        parts_per_consumer = len(self.topic.partitions) / len(participants)
        # TODO: deal with remainder
        remaining_ppc = len(self.topic.partitions) % len(participants)

        # 7. assign partitions from i*N to (i+1)*N - 1 to consumer Ci
        new_partitions = itertools.islice(
            self.topic.partitions,
            i * parts_per_consumer,
            (i + 1) * parts_per_consumer
        )

        new_partitions = set(
            PartitionName(p.broker.id, p.number) for p in new_partitions)

        old_partitions = self.partition_owner_registry.get()

        # 8. remove current entries from the partition owner registry
        self.partition_owner_registry.remove(
            old_partitions - new_partitions
        )

        # 9. add newly assigned partitions to the partition owner registry
        self.partition_owner_registry.add(
            new_partitions - old_partitions
        )
        print self.partition_owner_registry.get()


    @requires_configuration
    def __iter__(self):
        """
        Returns an iterator of messages.
        """
        raise NotImplementedError

    @requires_configuration
    def commit_offsets(self):
        """
        Commit the offsets of all messages consumed so far.
        """
        return
        raise NotImplementedError
