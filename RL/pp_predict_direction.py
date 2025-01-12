# Imports
import numpy as np
import os
import sys
import csv

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.cm as cm
import matplotlib.lines as mlines
import matplotlib.colors as colors
from matplotlib.lines import Line2D

import gym
from gym import spaces
from gym.wrappers import FlattenObservation

# Stable baselines 3
from stable_baselines3 import PPO
from stable_baselines3.common.env_checker import check_env

# Load custom functions
sys.path.append('./Fluid')
import field_velocity
import power_consumption
import bem_two_objects
import bem


def oseen_inverses(N1, squirmer_radius, target_radius, squirmer_position, target_position, epsilon, viscosity, coord_plane):
    """Calculate Oseen tensor inverses"""
    # Without target
    x1, y1, z1, dA = bem.canonical_fibonacci_lattice(N1, squirmer_radius)
    theta = np.arccos(z1 / squirmer_radius)
    phi = np.arctan2(y1, x1)
    x1_stack = np.stack((x1, y1, z1)).T
    A_oseen = bem.oseen_tensor_surface(x1_stack, dA, epsilon, viscosity)
    A_oseen_inv = np.linalg.inv(A_oseen)
    
    # With target
    N2 = int(4 * np.pi * target_radius ** 2 / dA)
    x2, y2, z2, _ = bem.canonical_fibonacci_lattice(N2, target_radius)
    x2_stack = np.stack((x2, y2, z2)).T
    if coord_plane == "xy":  # RL is 2d, Oseen i 3d 
        squirmer_position = np.append(squirmer_position, [0])
        target_position = np.append(target_position, [0])
    elif coord_plane == "yz":
        squirmer_position = np.append([0], squirmer_position)  
        target_position = np.append([0], target_position)
    elif coord_plane == "xz":
        squirmer_position = np.array([squirmer_position[0], 0, squirmer_position[1]])
        target_position = np.array([target_position[0], 0, target_position[1]])
        
    A_oseen_with = bem_two_objects.oseen_tensor_surface_two_objects(x1_stack, x2_stack, squirmer_position, target_position, dA, epsilon, viscosity)
    A_oseen_with_inv = np.linalg.inv(A_oseen_with)
    
    return x1_stack, N2, theta, phi, A_oseen_inv, A_oseen_with_inv


# Environment
class PredictDirectionEnv(gym.Env):
    """Gym environment for a predator-prey system in a fluid."""


    def __init__(self, N_surface_points, squirmer_radius, target_radius, max_mode, sensor_noise, viscosity, target_initial_position, reg_offset, coord_plane="yz"):
        #super().__init__() - ingen anelse om hvorfor jeg havde skrevet det eller hvor det kommer fra?
        # -- Variables --
        # Model
        self.N_surface_points = N_surface_points
        self.squirmer_radius = squirmer_radius
        self.target_radius = target_radius
        self.max_mode = max_mode # Max available legendre modes. 
        self.sensor_noise = sensor_noise
        self.coord_plane = coord_plane
        assert coord_plane in ["xy", "xz", "yz", None]
        self.epsilon = reg_offset  # Width of delta function blobs.
        self.viscosity = viscosity
        
        # -- Define action and observation space --
        # Actions: Strength of Legendre Modes
        if max_mode == 4:
            number_of_modes = 45  # Counted from power factors.
        elif max_mode == 3:
            number_of_modes = 27
        elif max_mode == 2: 
            number_of_modes = 13
        action_shape = (number_of_modes,)  # Weight of each mode.
        self.action_space = spaces.Box(low=-1, high=1, shape=action_shape, dtype=np.float32)

        # Observation is vector pointing in direction of average force
        self.observation_space = spaces.Box(low=-1, high=1, shape=(3,), dtype=np.float32) 

        # -- Calculate Oseen inverses --
        # Initial Positions
        self._agent_position = self._array_float([0, 0], shape=(2,))  # Agent in center
        self._target_position = self._array_float(target_initial_position, shape=(2,))

        self.x1_stack, self.N2, self.theta, self.phi, self.A_oseen_inv, self.A_oseen_with_inv = oseen_inverses(self.N_surface_points, self.squirmer_radius, self.target_radius, self._agent_position, 
                                                                                                               self._target_position, self.epsilon, self.viscosity, coord_plane)
        

    def _array_float(self, x, shape):  # Kan ændres til at shap bare tager x.shape
        """Helper function to input x into a shape sized array with dtype np.float32"""
        return np.array([x], dtype=np.float32).reshape(shape)


    def _average_force_difference(self, mode_array):
        # Find forces with and without target
        ux1, uy1, uz1 = field_velocity.field_cartesian_squirmer(self.max_mode, r=self.squirmer_radius, theta=self.theta, phi=self.phi, 
                                                                squirmer_radius=self.squirmer_radius, mode_array=mode_array,)
        u_comb = np.array([ux1, uy1, uz1]).ravel()
        u_comb_without = np.append(u_comb, np.zeros(6))  # No target
        u_comb_with = np.append(u_comb, np.zeros(12+3*self.N2))
        
        force_without = self.A_oseen_inv @ u_comb_without
        force_with = self.A_oseen_with_inv @ u_comb_with 
        
        # Differences
        N1 = self.N_surface_points
        dfx = force_with[:N1].T - force_without[:N1].T  # NOTE burde man allerede her tage abs()? Relatvant ift støj!?
        dfy = force_with[N1: 2*N1].T - force_without[N1: 2*N1].T
        dfz = force_with[2*N1: 3*N1].T - force_without[2*N1: 3*N1].T

        # Noise
        dfx += np.random.normal(loc=0, scale=self.sensor_noise, size=dfx.size)
        dfy += np.random.normal(loc=0, scale=self.sensor_noise, size=dfy.size)
        dfz += np.random.normal(loc=0, scale=self.sensor_noise, size=dfz.size)

        # Weight and force
        weight = np.sqrt(dfx ** 2 + dfy ** 2 + dfz ** 2)
        f_average = np.sum(weight[:, None] * self.x1_stack, axis=0)
        f_average_norm = f_average / np.linalg.norm(f_average, ord=2)
        return dfx, dfy, dfz, f_average_norm

        
    def _minimal_angle_difference(self, x, y):
        diff1 = x - y
        diff2 = diff1 + 2 * np.pi
        diff3 = diff1 - 2 * np.pi
        return np.min(np.abs([diff1, diff2, diff3]))
    
    
    def _reward(self, mode_array):
        # Calculate angle and average direction of change
        _, _, _, change_direction = self._average_force_difference(mode_array)

        agent_target_vec = self._target_position - self._agent_position  # Vector pointing from target to agent
        if self.coord_plane == "xz":
            angle = np.arctan2(agent_target_vec[0], agent_target_vec[1])  # Actual angle
            angle_largest_change = np.arctan2(change_direction[0], change_direction[2]) # Guess angle
        elif self.coord_plane == "xy":
            angle = np.arctan2(agent_target_vec[1], agent_target_vec[0])
            angle_largest_change = np.arctan2(change_direction[1], change_direction[0])
        elif self.coord_plane == "yz" or self.coord_plane == None:
            angle = np.arctan2(agent_target_vec[0], agent_target_vec[1])
            angle_largest_change = np.arctan2(change_direction[1], change_direction[2])

        # Reward is based on how close the angles are, closer is better
        angle_difference_norm = self._minimal_angle_difference(angle, angle_largest_change) / np.pi
        reward = 1 - angle_difference_norm
        return angle, angle_largest_change, reward
        

    def reset(self, seed=None):
        # Fix seed and reset values
        super().reset(seed=seed)
                                
        # Initial observation is no field
        observation = self._array_float([0, 0, 0], shape=(3,))

        return observation


    def step(self, action):
        # -- Action setup --
        # Actions are the available modes.
        mode_array = power_consumption.normalized_modes(action, self.max_mode, self.squirmer_radius, self.viscosity)
                        
        # -- Reward --
        angle, guessed_angle, reward = self._reward(mode_array)

        # -- Update values --
        _, _, _, x_change = self._average_force_difference(mode_array)
        observation = self._array_float(x_change, shape=(3,))
        info = {"angle": angle, "guessed angle": guessed_angle}
        done = True  # Only one time step as the system does not evolve over time
        
        return observation, reward, done, info

    
def train(N_surface_points, squirmer_radius, target_radius, max_mode, sensor_noise, viscosity, target_initial_position, reg_offset, coord_plane, train_total_steps, subfolder=None):
    env = PredictDirectionEnv(N_surface_points, squirmer_radius, target_radius, max_mode, sensor_noise, viscosity, target_initial_position, reg_offset, coord_plane)

    # Train with SB3
    log_path = os.path.join("RL", "Training", "Logs_direction")
    if subfolder != None:
        log_path = os.path.join(log_path, subfolder)
    model_path = os.path.join(log_path, "predict_direction")
    model = PPO("MlpPolicy", env, verbose=1, tensorboard_log=log_path)
    model.learn(total_timesteps=train_total_steps)
    model.save(model_path)
    
    # Save parameters in csv file
    file_path = os.path.join(log_path, "system_parameters.csv")
    with open(file_path, mode="w") as file:
        writer = csv.writer(file, delimiter=",")
        writer.writerow(["surface_points", "squirmer_radius", "target_radius", "max_mode ", "sensor_noise", 
                         "target_x1 ", "target_x2 ", "centers_distance", "viscosity", "regularization_offset", "coordinate_plane", "train_steps"])
        writer.writerow([N_surface_points, squirmer_radius, target_radius, max_mode, sensor_noise, target_initial_position[0], target_initial_position[1],
                         np.linalg.norm(target_initial_position, ord=2), viscosity, reg_offset, coord_plane, train_total_steps])
        

def mode_names(max_mode):
    """Get the name of the modes given the max mode in strings."""
    B_names = []
    B_tilde_names = []
    C_names = []
    C_tilde_names = []
    for i in range(max_mode+1):
        for j in range(i, max_mode+1):
            if j > 0:
                B_str = r"$B_{" + str(i) + str(j) + r"}$"
                B_names.append(B_str)
            if j > 1:
                C_str = r"$C_{" + str(i) + str(j) + r"}$"
                C_names.append(C_str)            
            if i > 0:
                B_tilde_str = r"$\tilde{B}_{" + str(i) + str(j) + r"}$"
                B_tilde_names.append(B_tilde_str)
                if j > 1:
                    C_tilde_str = r"$\tilde{C}_{" + str(i) + str(j) + r"}$"
                    C_tilde_names.append(C_tilde_str)
                    
    return B_names, B_tilde_names, C_names, C_tilde_names


def mode_iteration(N_iter, PPO_number, mode_lengths, subfolder=None):
    """Run environment N_iter times with training data from PPO_number directory."""
    # Load parameters and model, create environment
    if subfolder != None:
        parameters_path = f"RL/Training/Logs_direction/{subfolder}/PPO_{PPO_number}/system_parameters.csv"
        model_path = f"RL/Training/Logs_direction/{subfolder}/PPO_{PPO_number}/predict_direction"
    else:
        parameters_path = f"RL/Training/Logs_direction/PPO_{PPO_number}/system_parameters.csv"
        model_path = f"RL/Training/Logs_direction/PPO_{PPO_number}/predict_direction"

    parameters = np.genfromtxt(parameters_path, delimiter=",", names=True, dtype=None, encoding='UTF-8')
    N_surface_points = int(parameters["surface_points"])
    squirmer_radius = parameters["squirmer_radius"]
    target_radius = parameters["target_radius"]
    max_mode = parameters["max_mode"]
    sensor_noise = parameters["sensor_noise"]
    target_x1 = parameters["target_x1"]
    target_x2 = parameters["target_x2"]
    viscosity = parameters["viscosity"]
    reg_offset = parameters["regularization_offset"]
    coord_plane = parameters["coordinate_plane"]
    
    if coord_plane not in ["xy", "yz", "xz", None]:  # Backwards compatability when only yz plane was allowed
        coord_plane = "yz"
    
    model = PPO.load(model_path)
    env = PredictDirectionEnv(N_surface_points, squirmer_radius, target_radius, max_mode, sensor_noise, viscosity, np.array([target_x1, target_x2]), reg_offset, coord_plane)
    
    # Empty arrays for loop
    B_actions = np.empty((N_iter, mode_lengths[0]))
    B_tilde_actions = np.empty((N_iter, mode_lengths[1]))
    C_actions = np.empty((N_iter, mode_lengths[2]))
    C_tilde_actions = np.empty((N_iter, mode_lengths[3]))

    rewards = np.empty((N_iter))
    guessed_angles = np.empty((N_iter))

    # Run model N_iter times
    obs = env.reset()
    for i in range(N_iter):
        action, _ = model.predict(obs)
        obs, reward, _, info = env.step(action)
        rewards[i] = reward
        guessed_angles[i] = info["guessed angle"]
        mode_array = power_consumption.normalized_modes(action, max_mode, squirmer_radius, viscosity)
                
        B_actions[i, :] = mode_array[0][np.nonzero(mode_array[0])]
        B_tilde_actions[i, :] = mode_array[1][np.nonzero(mode_array[1])]
        C_actions[i, :] = mode_array[2][np.nonzero(mode_array[2])]
        C_tilde_actions[i, :] = mode_array[3][np.nonzero(mode_array[3])]
    
    return B_actions, B_tilde_actions, C_actions, C_tilde_actions, rewards, guessed_angles, parameters


def mode_choice_plot(max_mode, N_iter, PPO_number, subfolder=None):
    """Plot the modes taken at different iterations."""
    # Add more colors
    matplotlib.rcParams["axes.prop_cycle"] = matplotlib.cycler(color=['blue', 'green', 'red', 'cyan', 'magenta', 'yellow', 'black', 
                                                        'purple', 'pink', 'brown', 'orange', 'teal', 'coral', 'lightblue', 
                                                        'lime', 'lavender', 'turquoise', 'darkgreen', 'tan', 'salmon', 'gold'])

    # Names
    B_names, B_tilde_names, C_names, C_tilde_names = mode_names(max_mode)
    mode_lengths = [len(B_names), len(B_tilde_names), len(C_names), len(C_tilde_names)]
    B_actions, B_tilde_actions, C_actions, C_tilde_actions, rewards, guessed_angles, parameters = mode_iteration(N_iter, PPO_number, mode_lengths, subfolder)
    
    target_x1 = parameters["target_x1"]
    target_x2 = parameters["target_x2"]
    sensor_noise = parameters["sensor_noise"]
    guessed_angles = guessed_angles * 180 / np.pi
    angle = np.arctan2(target_x1, target_x2)
    
    # Plot
    def fill_axis(axis, y, marker, label, title):        
        axis.set(xticks=[], title=(title, 7), ylim=(-1, 1))
        axis.set_title(title, fontsize=7)
        axis.plot(y, marker=marker, ls="--", lw=0.75)
        axis.legend(label, fontsize=4, bbox_to_anchor=(1.05, 1), 
                    loc='upper left', borderaxespad=0.)
        
    # Define axis and fill them
    fig, ax = plt.subplots(nrows=2, ncols=2, dpi=200)
    ax1 = ax[0, 0]
    ax2 = ax[0, 1]
    ax3 = ax[1, 0]
    ax4 = ax[1, 1]

    fill_axis(ax1, B_actions, ".", B_names, title=r"$B$ weights")
    fill_axis(ax2, B_tilde_actions, ".", B_tilde_names, title=r"$\tilde{B}$ weights")
    fill_axis(ax3, C_actions, ".", C_names, title=r"$C$ weights")
    fill_axis(ax4, C_tilde_actions, ".", C_tilde_names, title=r"$\tilde{C}$ weights")
    
    # xticks
    xticks = []
    for reward, angle_guess in zip(rewards, guessed_angles):
        tick_str = f"R: {np.round(reward, 2)}, " + r"$\theta_g$: " + str(np.round(angle_guess, 2))
        xticks.append(tick_str)
        
    # General setup
    ax2.set(yticks=[])
    ax3.set(xlabel="Iteration", xticks=(np.arange(N_iter)))
    ax3.set_xticklabels(xticks, rotation=20, size=5)
    ax4.set(xlabel="Iteration", xticks=(np.arange(N_iter)), yticks=[])
    ax4.set_xticklabels(xticks, rotation=20, size=5)
    fig.suptitle(fr"Mode over iterations, Noise = {sensor_noise}, $\theta =${np.round(angle * 180 / np.pi, 2)}", fontsize=10)
    fig.tight_layout()
    
    # Save and show
    figname = f"noise{parameters['sensor_noise']}_maxmode{parameters['max_mode']}_targetradius{parameters['target_radius']}_distance{parameters['centers_distance']}_trainingsteps{parameters['train_steps']}.png"            
    plt.savefig("RL/Recordings/Images/" + figname)
    plt.show()


def mode_iteration_average_plot(max_mode, N_model_runs, PPO_list, changed_parameter, plot_reward=True, subfolder=None):
    """Plot the mode values average over N_model_runs on same training data against a variaed parameter determined by changed_parameter

    Args:
        max_mode (_type_): _description_
        N_model_runs (_type_): _description_
        PPO_list (_type_): _description_
        changed_parameter (_type_): _description_
        plot_reward (bool, optional): _description_. Defaults to True.
        subfolder (_type_, optional): _description_. Defaults to None.
    """
    assert changed_parameter in ["target_radius", "sensor_noise", "center_distance", "angle", "else"]

    # -- Data setup --
    B_names, B_tilde_names, C_names, C_tilde_names = mode_names(max_mode)
    mode_lengths = [len(B_names), len(B_tilde_names), len(C_names), len(C_tilde_names)]
    PPO_len = len(PPO_list)
        
    B_mean = np.empty((PPO_len, mode_lengths[0])) 
    B_tilde_mean = np.empty((PPO_len, mode_lengths[1])) 
    C_mean = np.empty((PPO_len, mode_lengths[2])) 
    C_tilde_mean = np.empty((PPO_len, mode_lengths[3])) 

    B_std = np.empty_like(B_mean) 
    B_tilde_std = np.empty_like(B_tilde_mean)
    C_std = np.empty_like(C_mean)
    C_tilde_std = np.empty_like(C_tilde_mean)
    
    reward_mean = np.empty(PPO_len)
    reward_std = np.empty_like(reward_mean)
    
    changed_parameter_list = np.empty(PPO_len)
    
    for i, PPO_val in enumerate(PPO_list):
        B_actions, B_tilde_actions, C_actions, C_tilde_actions, rewards, _, parameters = mode_iteration(N_model_runs, PPO_val, mode_lengths, subfolder)
        # Mean and std
        B_mean[i, :] = np.mean(B_actions, axis=0)
        B_tilde_mean[i, :] = np.mean(B_tilde_actions, axis=0)
        C_mean[i, :] = np.mean(C_actions, axis=0)
        C_tilde_mean[i, :] = np.mean(C_tilde_actions, axis=0)
        
        B_std[i, :] = np.std(B_actions, axis=0) / np.sqrt(N_model_runs - 1)
        B_tilde_std[i, :] = np.std(B_tilde_actions, axis=0) / np.sqrt(N_model_runs - 1)
        C_std[i, :] = np.std(C_actions, axis=0) / np.sqrt(N_model_runs - 1)
        C_tilde_std[i, :] = np.std(C_tilde_actions, axis=0) / np.sqrt(N_model_runs - 1)
        
        reward_mean[i] = np.mean(rewards)
        reward_std[i] = np.std(rewards) / np.sqrt(N_model_runs - 1)
        
        new_ticks = False
        if changed_parameter == "target_radius":
            changed_parameter_list[i] = parameters["target_radius"]  # Target radius
            xlabel = "Target Radius"
        elif changed_parameter == "sensor_noise":
            changed_parameter_list[i] = parameters["sensor_noise"]  # Sensor noise
            xlabel = "Sensor Noise"
        elif changed_parameter == "angle":
            coordinate_plane = parameters["coordinate_plane"]
            if coordinate_plane == "xy":
                xlabel = r"Initial angle $\phi_0$" 
                changed_parameter_list[i] = np.arctan2(parameters["target_x2"], parameters["target_x1"])  # arctan ( target x / target y ).
            elif coordinate_plane == "yz":
                xlabel = r"Initial angle $\theta_0$"
                changed_parameter_list[i] = np.arctan2(parameters["target_x1"], parameters["target_x2"])  # arctan ( target y / target z ).
            new_ticks = True
            xticks = np.arange(0, (2+1/4)*np.pi, np.pi/4)
            x_tick_labels = [r"$0$", r"$\pi/4$", r"$\pi/2$", r"$3\pi/4$",r"$\pi/2$", 
                      r"$5\pi/4$", r"3$\pi/2$", r"$7\pi/4$", r"$2\pi/4$",]
        elif changed_parameter == "center_distance":  # Target initial distance
            changed_parameter_list[i] = parameters["centers_distance"]  # Distance between the two centers
            xlabel = "Center-center distance"
        else:
            x = parameters["centers_distance"]/(parameters["target_radius"]+ parameters["squirmer_radius"])
            changed_parameter_list[i] = x
            xlabel = "else"
            

    def fill_axis(axis, y, sy, mode_name, title):        
        x_vals = changed_parameter_list
        sort_idx = np.argsort(x_vals)
        x_sort = x_vals[sort_idx]
        axis.set(title=(title, 7), ylim=(-0.5, 0.5))
        axis.set_title(title, fontsize=7)
        for i in range(y.shape[1]):
            y_sort = y[:, i][sort_idx]
            sy_sort = sy[:, i][sort_idx]
            axis.errorbar(x_sort, np.abs(y_sort), yerr=sy_sort, fmt=".--", lw=0.75)
        axis.legend(mode_name, fontsize=4, bbox_to_anchor=(1.05, 1), 
                    loc='upper left', borderaxespad=0.)
        axis.grid()
        if new_ticks == True:
            axis.set_xticks(ticks=xticks)
            axis.set_xticklabels(x_tick_labels)

    
    # -- Figure and axis setup --
    fig, ax = plt.subplots(nrows=2, ncols=2, dpi=200)
    axB = ax[0, 0]
    axBt = ax[0, 1]
    axC = ax[1, 0]
    axCt = ax[1, 1]
    
    fill_axis(axB, B_mean, B_std, B_names, r"$B$ modes")
    fill_axis(axBt, B_tilde_mean, B_tilde_std, B_tilde_names, title=r"$\tilde{B}$ modes")
    fill_axis(axC, C_mean, C_std, C_names, title=r"$C$ modes")
    fill_axis(axCt, C_tilde_mean, C_tilde_std, C_tilde_names, title=r"$\tilde{C}$ modes")
            
    # General setup
    axBt.set_yticklabels([])
    axC.set(xlabel=xlabel)
    axCt.set(xlabel=xlabel, yticklabels=[])
    fig.suptitle(fr"Average mode values over {xlabel}", fontsize=10)
    fig.tight_layout()
    
    # Save and show
    figname = f"average_modes_maxmode{max_mode}_{xlabel}{changed_parameter_list}.png"
    #plt.savefig("RL/Recordings/Images/" + figname)
    plt.show()
    
    if plot_reward:
        figr, axr = plt.subplots(dpi=200)
        axr.errorbar(changed_parameter_list, reward_mean, yerr=reward_std, fmt=".")
        axr.set(xlabel=xlabel, ylabel="Reward", title="Mean reward")
        figr.tight_layout()
        plt.show()


def plot_modes_one_graph(B_idx, Bt_idx, C_idx, Ct_idx, max_mode, N_model_runs, PPO_list, changed_parameter, subfolder=None):
    # Kræver at man manuelt specificerer hvilke modes

    assert changed_parameter in ["target_radius", "sensor_noise", "center_distance", "angle", "radii_sum"]
    B_names, B_tilde_names, C_names, C_tilde_names = mode_names(max_mode)
    mode_lengths = [len(B_names), len(B_tilde_names), len(C_names), len(C_tilde_names)]
    PPO_len = len(PPO_list)
        
    B_mean = np.empty((PPO_len, mode_lengths[0])) 
    B_tilde_mean = np.empty((PPO_len, mode_lengths[1])) 
    C_mean = np.empty((PPO_len, mode_lengths[2])) 
    C_tilde_mean = np.empty((PPO_len, mode_lengths[3])) 

    B_std = np.empty_like(B_mean) 
    B_tilde_std = np.empty_like(B_tilde_mean)
    C_std = np.empty_like(C_mean)
    C_tilde_std = np.empty_like(C_tilde_mean)
    
    reward_mean = np.empty(PPO_len)
    reward_std = np.empty_like(reward_mean)
    
    changed_parameter_list = np.empty(PPO_len)
    
    # Fill out the data arrays
    for i, PPO_val in enumerate(PPO_list):
        B_actions, B_tilde_actions, C_actions, C_tilde_actions, rewards, _, parameters = mode_iteration(N_model_runs, PPO_val, mode_lengths, subfolder)
        # Mean and std
        B_mean[i, :] = np.mean(B_actions, axis=0)
        B_tilde_mean[i, :] = np.mean(B_tilde_actions, axis=0)
        C_mean[i, :] = np.mean(C_actions, axis=0)
        C_tilde_mean[i, :] = np.mean(C_tilde_actions, axis=0)
        
        B_std[i, :] = np.std(B_actions, axis=0) / np.sqrt(N_model_runs - 1)
        B_tilde_std[i, :] = np.std(B_tilde_actions, axis=0) / np.sqrt(N_model_runs - 1)
        C_std[i, :] = np.std(C_actions, axis=0) / np.sqrt(N_model_runs - 1)
        C_tilde_std[i, :] = np.std(C_tilde_actions, axis=0) / np.sqrt(N_model_runs - 1)
        
        reward_mean[i] = np.mean(rewards)
        reward_std[i] = np.std(rewards) / np.sqrt(N_model_runs - 1)
        
        new_ticks = False
        new_xlim = False
        if changed_parameter == "target_radius":
            changed_parameter_list[i] = parameters["target_radius"]  # Target radius
            xlabel = r"$a_{target}$"
            title = ""
        elif changed_parameter == "sensor_noise":
            changed_parameter_list[i] = parameters["sensor_noise"]  # Sensor noise
            xlabel = r"$\sigma_{noise}$"
            title = ""
        elif changed_parameter == "angle":
            coordinate_plane = parameters["coordinate_plane"]
            # Depending on which coordinate plane, angle is calculated differently
            if coordinate_plane == "xy":
                xlabel = r"$\phi_0$" 
                changed_parameter_list[i] = np.arctan2(parameters["target_x2"], parameters["target_x1"])  # arctan ( target y / target x ).
                title = r"$xy$-plane"
            elif coordinate_plane == "yz":
                xlabel = r"$\theta_0$"
                changed_parameter_list[i] = np.arctan2(parameters["target_x1"], parameters["target_x2"])  # arctan ( target y / target z ).
                title = r"$yz$-plane"
            else:  # xz
                xlabel = r"$\theta_0$"
                changed_parameter_list[i] = np.arctan2(parameters["target_x1"], parameters["target_x2"])  # arctan (target x / target z)
                title = r"$xz$-plane"
            # Give specific ticks for angle, radians
            new_ticks = True
            xticks = np.arange(-np.pi, (5/4)*np.pi, np.pi/4)
            x_tick_labels = [r"$-\pi$", r"$\frac{-3\pi}{4}$", r"$\frac{-\pi}{2}$", r"$\frac{-\pi}{4}$", r"$0$", r"$\frac{\pi}{4}$", r"$\frac{\pi}{2}$", r"$\frac{3\pi}{4}$", r"$\pi$"]  # [-pi, pi]
        elif changed_parameter == "center_distance":  # Target initial distance
            changed_parameter_list[i] = parameters["centers_distance"]  # Distance between the two centers
            xlabel = r"$r_0$"
            title = ""
            new_xlim = True
            xlim = (1.35, 2.75)
        elif changed_parameter == "radii_sum":
            changed_parameter_list[i] = parameters["centers_distance"] / (parameters["target_radius"] + parameters["squirmer_radius"])
            xlabel = r"$\beta$"
            title= r"$a_{target} = $" + str(parameters["target_radius"])
            new_xlim = True
            xlim = (1, 1.75)
    
    # Include only desired modes
    B_mean_plot = B_mean[:, B_idx]
    B_std_plot = B_std[:, B_idx]    
    B_label = [B_names[i] for i in B_idx]

    Bt_mean_plot = B_tilde_mean[:, Bt_idx]
    Bt_std_plot = B_tilde_std[:, Bt_idx]
    Bt_label = [B_tilde_names[i] for i in Bt_idx]

    C_mean_plot = C_mean[:, C_idx]
    C_std_plot = C_std[:, C_idx]    
    C_label = [C_names[i] for i in C_idx]

    Ct_mean_plot = C_tilde_mean[:, Ct_idx]
    Ct_std_plot = C_tilde_std[:, Ct_idx]
    Ct_label = [C_tilde_names[i] for i in Ct_idx]
    
    sort_idx = np.argsort(changed_parameter_list)
    x_sort = changed_parameter_list[sort_idx]
    # Add additional point to angle graph, depends on data / coord plane
    if changed_parameter == "angle":
        if coordinate_plane == "yz":
            # Add pi to x data, update sort_idx
            x_sort = np.append(x_sort, -x_sort[0])
            sort_idx = np.append(sort_idx, sort_idx[0]) 
        elif coordinate_plane == "xy":
            # Add -pi
            x_sort = np.append(-x_sort[-1], x_sort)
            sort_idx = np.append(sort_idx[-1], sort_idx)
        else:  # xz
            # Add pi
            x_sort = np.append(x_sort, -x_sort[0])
            sort_idx = np.append(sort_idx, sort_idx[0])             
        
    # -- Start figure --
    fig, ax = plt.subplots(figsize=(10, 3))
    ax.set(xlabel=xlabel, ylabel="Absolute Mode Value", title=title)
    if new_xlim:
        ax.set(xlim=xlim)
    
    from itertools import cycle
    marker_list = ["o", "^", "s", "p", "P", "*","d", "x", "X", ">", "H", "+"]
    ls_list = ["solid", "dotted", "dashed", "dashdot", (0, (1, 1))]
    marker_cycle = cycle(marker_list)
    ls_cycle = cycle(ls_list)


    def plot_mode(y, sy, label):   
        for i in range(np.shape(y)[1]):
            y_sort = y[:, i][sort_idx]
            sy_sort = sy[:, i][sort_idx]
            ax.errorbar(x_sort, np.abs(y_sort), yerr=sy_sort, lw=0.85, markersize=10, label=label[i], marker=next(marker_cycle),)# linestyle=next(ls_cycle)) #marker=marker_list[i],)
    
    
    # Run the plotting function
    plot_mode(Bt_mean_plot, Bt_std_plot, Bt_label)
    plot_mode(C_mean_plot, C_std_plot, C_label)
    plot_mode(Ct_mean_plot, Ct_std_plot, Ct_label)
    plot_mode(B_mean_plot, B_std_plot, B_label)  # HUSK AT SÆTTE TILBAGE!
    
    ax.legend(fontsize=8, bbox_to_anchor=(1.02, 1), 
                loc='upper left', borderaxespad=0.)
    if new_ticks == True:
        ax.set_xticks(ticks=xticks)
        ax.set_xticklabels(x_tick_labels)    
    ax.grid()    
    figname = "RL/Recordings/Images/" + f"mode_one_graph_{title}.png"
    plt.savefig(figname, dpi=300, bbox_inches="tight")
    plt.show()
    

# -- Run the code --
if __name__ == "__main__":
    # Model Parameters
    N_surface_points = 1300
    squirmer_radius = 1
    target_radius = 0.25
    tot_radius = squirmer_radius + target_radius
    target_initial_position = [2, 2] / np.sqrt(2)
    max_mode = 2
    viscosity = 1
    sensor_noise = 0.05
    reg_offset = 0.05
    coord_plane = "yz"

    
    def check_model(N_surface_points, squirmer_radius, target_radius, max_mode, sensor_noise, viscosity, target_initial_position, reg_offset, coord_plane):
        env = PredictDirectionEnv(N_surface_points, squirmer_radius, target_radius, max_mode, sensor_noise, viscosity, target_initial_position, reg_offset, coord_plane)
        print("-- SB3 CHECK ENV: --")
        if check_env(env) == None:
            print("   The Environment is compatible with SB3")
        else:
            print(check_env(env))

    
    check_model(N_surface_points, squirmer_radius, target_radius, max_mode, sensor_noise, viscosity, target_initial_position, reg_offset, coord_plane)
    
#"target_radius", "noise", "center_distance", "angle", "else"
# If wants to see reward over time, write the following in cmd in the log directory
# tensorboard --logdir=.