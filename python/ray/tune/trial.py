from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys
import traceback
import ray

from collections import namedtuple
from ray.rllib.agents import get_agent_class


# Ray resources required to schedule a Trial
Resources = namedtuple("Resources", ["cpu", "gpu"])


class Trial(object):
    """A trial object holds the state for one model training run.

    Trials are themselves managed by the TrialRunner class, which implements
    the event loop for submitting trial runs to a Ray cluster.

    Trials start in the PENDING state, and transition to RUNNING once started.
    On error it transitions to ERROR, otherwise TERMINATED on success.
    """

    PENDING = 'PENDING'
    RUNNING = 'RUNNING'
    TERMINATED = 'TERMINATED'
    ERROR = 'ERROR'

    def __init__(
            self, env_creator, alg, config={}, local_dir='/tmp/ray',
            agent_id=None, resources=Resources(cpu=1, gpu=0),
            stopping_criterion={}, checkpoint_freq=sys.maxsize,
            restore_path=None, upload_dir=None):
        """Initialize a new trial.

        The args here take the same meaning as the command line flags defined
        in ray.tune.config_parser.
        """

        # Immutable config
        self.env_creator = env_creator
        if type(env_creator) is str:
            self.env_name = env_creator
        else:
            self.env_name = "custom"
        self.alg = alg
        self.config = config
        self.local_dir = local_dir
        self.agent_id = agent_id
        self.resources = resources
        self.stopping_criterion = stopping_criterion
        self.checkpoint_freq = checkpoint_freq
        self.restore_path = restore_path
        self.upload_dir = upload_dir

        # Local trial state that is updated during the run
        self.last_result = None
        self.checkpoint_path = None
        self.agent = None
        self.status = Trial.PENDING

    def start(self):
        """Starts this trial.

        If an error is encountered when starting the trial, an exception will
        be thrown.
        """

        self.status = Trial.RUNNING
        agent_cls = get_agent_class(self.alg)
        cls = ray.remote(
            num_cpus=self.resources.cpu, num_gpus=self.resources.gpu)(
                agent_cls)
        self.agent = cls.remote(
            self.env_creator, self.config, self.local_dir, self.upload_dir,
            agent_id=self.agent_id)
        if self.restore_path:
            ray.get(self.agent.restore.remote(self.restore_path))

    def stop(self, error=False):
        """Stops this trial.

        Stops this trial, releasing all allocating resources. If stopping the
        trial fails, the run will be marked as terminated in error, but no
        exception will be thrown.

        Args:
            error (bool): Whether to mark this trial as terminated in error.
        """

        if error:
            self.status = Trial.ERROR
        else:
            self.status = Trial.TERMINATED

        try:
            if self.agent:
                self.agent.stop.remote()
                self.agent.__ray_terminate__.remote(
                    self.agent._ray_actor_id.id())
        except:
            print("Error stopping agent:", traceback.format_exc())
            self.status = Trial.ERROR
        finally:
            self.agent = None

    def train_remote(self):
        """Returns Ray future for one iteration of training."""

        assert self.status == Trial.RUNNING, self.status
        return self.agent.train.remote()

    def should_stop(self, result):
        """Whether the given result meets this trial's stopping criteria."""

        for criteria, stop_value in self.stopping_criterion.items():
            if getattr(result, criteria) >= stop_value:
                return True

        return False

    def should_checkpoint(self):
        """Whether this trial is due for checkpointing."""

        if self.checkpoint_freq is None:
            return False

        return self.last_result.training_iteration % self.checkpoint_freq == 0

    def progress_string(self):
        """Returns a progress message for printing out to the console."""

        if self.last_result is None:
            return self.status
        return '{}, {} s, {} ts, {} itrs, {} rew'.format(
            self.status,
            int(self.last_result.time_total_s),
            int(self.last_result.timesteps_total),
            self.last_result.training_iteration,
            round(self.last_result.episode_reward_mean, 1))

    def checkpoint(self):
        """Synchronously checkpoints the state of this trial.

        TODO(ekl): we should support a PAUSED state based on checkpointing.
        """

        path = ray.get(self.agent.save.remote())
        self.checkpoint_path = path
        print("Saved checkpoint to:", path)

        return path

    def __str__(self):
        identifier = '{}_{}'.format(self.alg, self.env_name)
        if self.agent_id:
            identifier += '_' + self.agent_id
        return identifier

    def __eq__(self, other):
        return str(self) == str(other)

    def __hash__(self):
        return hash(str(self))
