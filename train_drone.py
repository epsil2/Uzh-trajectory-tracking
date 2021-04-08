import os
import json
import time
import numpy as np
import torch
import torch.nn.functional as F

from neural_control.dataset import QuadDataset
from train_base import TrainBase
from neural_control.drone_loss import (
    drone_loss_function, simply_last_loss, reference_loss, mse_loss,
    weighted_loss
)
from neural_control.dynamics.quad_dynamics_simple import SimpleDynamics
from neural_control.dynamics.quad_dynamics_flightmare import (
    FlightmareDynamics
)
from neural_control.dynamics.quad_dynamics_trained import LearntDynamics
from neural_control.controllers.network_wrapper import NetworkWrapper
from neural_control.environments.drone_env import QuadRotorEnvBase
from evaluate_drone import QuadEvaluator
from neural_control.models.hutter_model import Net
try:
    from neural_control.flightmare import FlightmareWrapper
except ModuleNotFoundError:
    pass


class TrainDrone(TrainBase):
    """
    Train a controller for a quadrotor
    """

    def __init__(self, train_dynamics, eval_dynamics, config):
        """
        param sample_in: one of "train_env", "eval_env", "real_flightmare"
        """
        self.config = config
        super().__init__(train_dynamics, eval_dynamics, **config)

        # Create environment for evaluation
        if self.sample_in == "real_flightmare":
            self.eval_env = FlightmareWrapper(self.delta_t)
        elif self.sample_in == "eval_env":
            self.eval_env = QuadRotorEnvBase(self.eval_dynamics, self.delta_t)
        elif self.sample_in == "train_env":
            self.eval_env = QuadRotorEnvBase(self.train_dynamics, self.delta_t)
        else:
            raise ValueError(
                "sample in must be one of eval_env, train_env, real_flightmare"
            )

    def initialize_model(
        self,
        base_model=None,
        modified_params={},
        base_model_name="model_quad"
    ):
        # Load model or initialize model
        if base_model is not None:
            self.net = torch.load(os.path.join(base_model, base_model_name))
            # load std or other parameters from json
            with open(
                os.path.join(base_model, "param_dict.json"), "r"
            ) as outfile:
                self.param_dict = json.load(outfile)
            STD = np.array(self.param_dict["std"]).astype(float)
            MEAN = np.array(self.param_dict["mean"]).astype(float)
        else:
            self.state_data = QuadDataset(
                self.epoch_size,
                self.self_play,
                reset_strength=self.reset_strength,
                max_drone_dist=self.max_drone_dist,
                ref_length=self.nr_actions,
                dt=self.delta_t
            )
            in_state_size = self.state_data.normed_states.size()[1]
            # +9 because adding 12 things but deleting position (3)
            self.net = Net(
                in_state_size,
                self.nr_actions,
                self.ref_dim,
                self.action_dim * self.nr_actions,
                conv=1
            )
            (STD, MEAN) = (self.state_data.std, self.state_data.mean)

        # save std for normalization during test time
        self.param_dict = {"std": STD.tolist(), "mean": MEAN.tolist()}
        # update the used parameters:
        self.param_dict["reset_strength"] = self.reset_strength
        self.param_dict["max_drone_dist"] = self.max_drone_dist
        self.param_dict["horizon"] = self.nr_actions
        self.param_dict["ref_length"] = self.nr_actions
        self.param_dict["thresh_div"] = self.thresh_div_start
        self.param_dict["dt"] = self.delta_t
        self.param_dict["take_every_x"] = self.self_play_every_x
        self.param_dict["thresh_stable"] = self.thresh_stable_start
        self.param_dict["speed_factor"] = self.speed_factor
        for k, v in modified_params.items():
            if type(v) == np.ndarray:
                modified_params[k] = v.tolist()
        self.param_dict["modified_params"] = modified_params

        with open(
            os.path.join(self.save_path, "param_dict.json"), "w"
        ) as outfile:
            json.dump(self.param_dict, outfile)

        # init dataset
        self.state_data = QuadDataset(
            self.epoch_size, self.self_play, **self.param_dict
        )
        self.init_optimizer()

    def train_controller_model(
        self, current_state, action_seq, in_ref_states, ref_states
    ):
        # zero the parameter gradients
        self.optimizer_controller.zero_grad()
        # save the reached states
        intermediate_states = torch.zeros(
            current_state.size()[0], self.nr_actions, self.state_size
        )
        for k in range(self.nr_actions):
            # extract action
            action = action_seq[:, k]
            current_state = self.train_dynamics(
                current_state, action, dt=self.delta_t
            )
            intermediate_states[:, k] = current_state

        loss = simply_last_loss(
            intermediate_states, ref_states[:, -1], action_seq, printout=0
        )

        # Backprop
        loss.backward()
        self.optimizer_controller.step()
        return loss

    def evaluate_model(self, epoch):
        # EVALUATE
        print(f"\nEpoch {epoch} (before)")
        controller = NetworkWrapper(
            self.net, self.state_data, **self.param_dict
        )

        evaluator = QuadEvaluator(controller, self.eval_env, **self.param_dict)
        # run with mpc to collect data
        # eval_env.run_mpc_ref("rand", nr_test=5, max_steps=500)
        # run without mpc for evaluation
        with torch.no_grad():
            suc_mean, suc_std = evaluator.eval_ref(
                "rand",
                nr_test=10,
                max_steps=self.max_steps,
                **self.param_dict
            )

        self.sample_new_data(epoch)

        # increase threshold
        if epoch % 5 == 0 and self.param_dict["thresh_div"
                                              ] < self.thresh_div_end:
            self.param_dict["thresh_div"] += .05
            print(
                "increased thresh div",
                round(self.param_dict["thresh_div"], 2)
            )

        # save best model
        self.save_model(epoch, suc_mean)

        self.mean_list.append(suc_mean)
        self.std_list.append(suc_std)
        return suc_mean, suc_std


def train_control(base_model, config):
    """
    Train a controller from scratch or with an initial model
    """
    modified_params = config["modified_params"]
    # TODO: might be problematic
    train_dynamics = FlightmareDynamics(**modified_params)
    eval_dynamics = FlightmareDynamics(**modified_params)

    # make sure that also the self play samples are collected in same env
    config["sample_in"] = "train_env"

    trainer = TrainDrone(train_dynamics, eval_dynamics, config)
    trainer.initialize_model(base_model, modified_params=modified_params)

    trainer.run_control(config)


def train_dynamics(base_model, config):
    """First train dynamcs, then train controller with estimated dynamics

    Args:
        base_model (filepath): Model to start training with
        config (dict): config parameters
    """
    modified_params = config["modified_params"]
    config["sample_in"] = "train_env"

    # train environment is learnt
    train_dynamics = LearntDynamics()
    eval_dynamics = FlightmareDynamics(**modified_params)

    trainer = TrainDrone(train_dynamics, eval_dynamics, config)
    trainer.initialize_model(base_model, modified_params=modified_params)

    # RUN
    trainer.run_dynamics(config)


def train_sampling_finetune(base_model, config):
    """First train dynamcs, then train controller with estimated dynamics

    Args:
        base_model (filepath): Model to start training with
        config (dict): config parameters
    """
    modified_params = config["modified_params"]
    config["sample_in"] = "eval_env"

    # train environment is learnt
    train_dynamics = FlightmareDynamics()
    eval_dynamics = FlightmareDynamics(**modified_params)

    trainer = TrainDrone(train_dynamics, eval_dynamics, config)
    trainer.initialize_model(base_model, modified_params=modified_params)

    # RUN
    trainer.run_control(config, sampling_based_finetune=True)


if __name__ == "__main__":
    # LOAD CONFIG
    with open("configs/quad_config.json", "r") as infile:
        config = json.load(infile)

    mod_params = {"translational_drag": np.array([.3, .3, .3])}
    config["modified_params"] = mod_params

    baseline_model = "trained_models/quad/baseline_flightmare"

    # TRAIN
    # train_control(baseline_model, config)
    train_dynamics(baseline_model, config)
    # train_sampling_finetune(baseline_model, config)
    # FINE TUNING parameters:
    # self.thresh_div_start = 1
    # self.self_play = 1.5
    # self.epoch_size = 500
    # self.max_steps = 1000
    # self.self_play_every_x = 5
    # self.learning_rate = 0.0001
