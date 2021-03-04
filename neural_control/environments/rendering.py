import numpy as np
from neural_control.environments.copter import Euler


def body_to_world_matrix(euler):
    """
    Creates a transformation matrix for directions from a body frame
    to world frame for a body with attitude given by `euler` Euler angles.
    :param euler: The Euler angles of the body frame.
    :return: The transformation matrix.
    """
    return np.transpose(world_to_body_matrix(euler))


def world_to_body_matrix(euler):
    """
    Creates a transformation matrix for directions from world frame
    to body frame for a body with attitude given by `euler` Euler angles.
    :param euler: The Euler angles of the body frame.
    :return: The transformation matrix.
    """

    # check if we have a cached result already available
    matrix = euler.get_from_cache("world_to_body")
    if matrix is not None:
        return matrix

    roll = euler.roll
    pitch = euler.pitch
    yaw = euler.yaw

    Cy = np.cos(yaw)
    Sy = np.sin(yaw)
    Cp = np.cos(pitch)
    Sp = np.sin(pitch)
    Cr = np.cos(roll)
    Sr = np.sin(roll)

    matrix = np.array(
        [
            [Cy * Cp, Sy * Cp, -Sp],
            [Cy * Sp * Sr - Cr * Sy, Cr * Cy + Sr * Sy * Sp, Cp * Sr],
            [Cy * Sp * Cr + Sr * Sy, Cr * Sy * Sp - Cy * Sr, Cr * Cp]
        ]
    )

    euler.add_to_cache("world_to_body", matrix)

    return matrix


def body_to_world(euler, vector):
    """
    Transforms a direction `vector` from body to world coordinates,
    where the body frame is given by the Euler angles `euler.
    :param euler: Euler angles of the body frame.
    :param vector: The direction vector to transform.
    :return: Direction in world frame.
    """
    return np.dot(body_to_world_matrix(euler), vector)


class Renderer:

    def __init__(self):
        self.viewer = None
        self.center = None

        self.scroll_speed = 0.1
        self.objects = []

    def draw_line_2d(self, start, end, color=(0, 0, 0)):
        self.viewer.draw_line(start, end, color=color)

    def draw_line_3d(self, start, end, color=(0, 0, 0)):
        self.draw_line_2d((start[0], start[2]), (end[0], end[2]), color=color)

    def draw_circle(
        self, position, radius, color, filled=True
    ):  # pragma: no cover
        from gym.envs.classic_control import rendering
        copter = rendering.make_circle(radius, filled=filled)
        copter.set_color(*color)
        if len(position) == 3:
            position = (position[0], position[2])
        copter.add_attr(rendering.Transform(translation=position))
        self.viewer.add_onetime(copter)

    def draw_polygon(self, v, filled=False):
        from gym.envs.classic_control import rendering
        airplane = rendering.make_polygon(v, filled=filled)
        self.viewer.add_onetime(airplane)

    def add_object(self, new):
        self.objects.append(new)

    def set_center(self, new_center):
        # new_center is None => We are resetting.
        if new_center is None:
            self.center = None
            return

        # self.center is None => First step, jump to target
        if self.center is None:
            self.center = new_center

        # otherwise do soft update.
        self.center = (
            1.0 - self.scroll_speed
        ) * self.center + self.scroll_speed * new_center
        if self.viewer is not None:
            self.viewer.set_bounds(-7 + self.center, 7 + self.center, -1, 13)

    def setup(self):
        from gym.envs.classic_control import rendering
        if self.viewer is None:
            self.viewer = rendering.Viewer(500, 500)

    def render(self, mode='human', close=False):
        if close:
            self.close()
            return

        if self.viewer is None:
            self.setup()

        for draw_ob in self.objects:  # type RenderedObject
            draw_ob.draw(self)

        return self.viewer.render(return_rgb_array=(mode == 'rgb_array'))

    def close(self):
        if self.viewer is not None:
            self.viewer.close()
            self.viewer = None


class RenderedObject:

    def draw(self, renderer: Renderer):
        raise NotImplementedError()


class Ground(RenderedObject):  # pragma: no cover

    def __init__(self, step_size=2):
        self._step_size = step_size

    def draw(self, renderer):
        """ Draws the ground indicator.
        """
        center = renderer.center
        renderer.draw_line_2d((-10 + center, 0.0), (10 + center, 0.0))
        pos = round(center / self._step_size) * self._step_size

        for i in range(-8, 10, self._step_size):
            renderer.draw_line_2d((pos + i, 0.0), (pos + i - 2, -2.0))


class QuadCopter(RenderedObject):  # pragma: no cover

    def __init__(self, source):
        self.source = source
        self._show_thrust = True

    def draw(self, renderer):
        status = self.source._state
        setup = self.source.setup

        # transformed main axis
        trafo = status.attitude

        # draw current orientation
        rotated = body_to_world(trafo, [0, 0, 0.5])
        renderer.draw_line_3d(status.position, status.position + rotated)

        self.draw_propeller(
            renderer, trafo, status.position, [1, 0, 0],
            status.rotor_speeds[0] / setup.max_rotor_speed
        )
        self.draw_propeller(
            renderer, trafo, status.position, [0, 1, 0],
            status.rotor_speeds[1] / setup.max_rotor_speed
        )
        self.draw_propeller(
            renderer, trafo, status.position, [-1, 0, 0],
            status.rotor_speeds[2] / setup.max_rotor_speed
        )
        self.draw_propeller(
            renderer, trafo, status.position, [0, -1, 0],
            status.rotor_speeds[3] / setup.max_rotor_speed
        )

    @staticmethod
    def draw_propeller(
        renderer, euler, position, propeller_position, rotor_speed
    ):
        structure_line = body_to_world(euler, propeller_position)
        renderer.draw_line_3d(position, position + structure_line)
        renderer.draw_circle(position + structure_line, 0.1, (0, 0, 0))
        thrust_line = body_to_world(euler, [0, 0, -0.5 * rotor_speed**2])
        renderer.draw_line_3d(
            position + structure_line, position + structure_line + thrust_line
        )


class FixedWingDrone(RenderedObject):

    def __init__(self, source):
        self.source = source
        self._show_thrust = True
        self.target = [100, 0]

    def set_target(self, target):
        self.target = target
        self.x_normalize = 14 / self.target[0]

    def draw(self, renderer):
        status = self.source._state.copy()
        max_rotor_speed = 1000

        # transformed main axis
        trafo = Euler(0, status[4], 0)

        # normalize x to have drone between left and right bound
        # and set z to other way round
        position = [-7 + status[0] * self.x_normalize, 0, status[1] * (-1)]

        # draw target point
        renderer.draw_circle(
            (6.8, self.target[1] * (-1)), .2, (0, 1, 0), filled=True
        )

        self.draw_airplane(renderer, position, trafo)

    @staticmethod
    def draw_airplane(renderer, position, euler):
        # plaen definition
        offset = np.array([-5, 0, -1.5])
        scale = .3
        coord_plane = (
            np.array(
                [
                    [1, 0, 1], [1, 0, 3.3], [2, 0, 2], [5.5, 0, 2],
                    [5, 0, 2.5], [4.5, 0, 2], [8, 0, 2], [10, 0, 1], [1, 0, 1]
                ]
            ) + offset
        ) * scale
        coord_wing = (
            np.array([[4, 0, 1.5], [5, 0, 0], [6, 0, 1.5]]) + offset
        ) * scale

        rot_matrix = body_to_world_matrix(euler)
        coord_plane_rotated = (
            np.array([np.dot(rot_matrix, coord)
                      for coord in coord_plane]) + position
        )[:, [0, 2]]
        coord_wing_rotated = (
            np.array([np.dot(rot_matrix, coord)
                      for coord in coord_wing]) + position
        )[:, [0, 2]]

        renderer.draw_polygon(coord_plane_rotated)
        renderer.draw_polygon(coord_wing_rotated)