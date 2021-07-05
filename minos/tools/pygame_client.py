import argparse
import copy
import math
import numpy as np
import pandas as pd
from pathlib import Path
import matplotlib.pyplot as plt
import pygame
from pygame.locals import *
from timeit import default_timer as timer
import traceback
import random, time

from minos.lib import common
from minos.config.sim_args import parse_sim_args
from minos.lib.Simulator import Simulator
from minos.lib.util.ActionTraces import ActionTraces
from minos.lib.util.StateSet import StateSet
from minos.lib.util.VideoWriter import VideoWriter


REPLAY_MODES = ['actions', 'positions']
VIDEO_WRITER = None
TMP_SURFS = {}

def get_angle_rad(x_vector,y_vector):
    angle = 0.0
    if abs(x_vector) > 0:
        angle = math.atan2(y_vector, x_vector)
        if angle >0:
            angle=angle-math.pi
        else:
            angle=angle+math.pi
    return angle


def get_angle(x_vector,y_vector):
    angle = 0.0
    if abs(x_vector) > 0:
        angle = math.atan2(y_vector, x_vector)*180/math.pi
        if angle >0:
            angle=angle-180
        else:
            angle=angle+180
    return angle

def blit_img_to_surf(img, surf, position=(0, 0), surf_key='*'):
    global TMP_SURFS
    if len(img.shape) == 2:  # gray (y)
        img = np.dstack([img, img, img, np.ones(img.shape, dtype=np.uint8)*255])  # y -> yyy1
    else:
        img = img[:, :, [2, 1, 0, 3]]  # bgra -> rgba
    img_shape = (img.shape[0], img.shape[1])
    TMP_SURF = TMP_SURFS.get(surf_key)
    if not TMP_SURF or TMP_SURF.get_size() != img_shape:
        # print('create new surf %dx%d' % img_shape)
        TMP_SURF = pygame.Surface(img_shape, 0, 32)
        TMP_SURFS[surf_key] = TMP_SURF
    bv = TMP_SURF.get_view("0")
    bv.write(img.tostring())
    del bv
    surf.blit(TMP_SURF, position)


def display_episode_info(episode_info, display_surf, camera_outputs, show_goals=False):
    displayed = episode_info.get('displayed',0)
    if displayed < 1:
        print('episode_info', {k: episode_info[k] for k in episode_info if k != 'goalObservations'})
        if show_goals and 'goalObservations' in episode_info:
            # NOTE: There can be multiple goals with separate goal observations for each
            # We currently just handle one
            goalObservations = episode_info['goalObservations']
            if len(goalObservations) > 0:
                # Call display_response but not write to video
                display_response(goalObservations[0], display_surf, camera_outputs, print_observation=False, write_video=False)
        episode_info['displayed'] = displayed + 1


def draw_forces(forces, display_surf, area):
    r = 5
    size = round(0.45*min(area.width, area.height)-r)
    center = area.center
    pygame.draw.rect(display_surf, (0,0,0), area, 0)  # fill with black
    # assume forces are radially positioned evenly around agent
    # TODO: Actual get force sensor positions and visualize them
    dt = -2*math.pi/forces.shape[0]
    theta = math.pi/2
    for i in range(forces.shape[0]):
        x = round(center[0] + math.cos(theta)*size)
        y = round(center[1] + math.sin(theta)*size)
        width = 0 if forces[i] else 1
        pygame.draw.circle(display_surf, (255,255,0), (x,y), r, width)
        theta += dt

def draw_offset(offset, display_surf, area, color=(0,0,255)):
    dir = (offset[0], offset[2])
    mag = math.sqrt(dir[0]*dir[0] + dir[1]*dir[1])
    if mag:
        dir = (dir[0]/mag, dir[1]/mag)
    size = round(0.45*min(area.width, area.height))
    center = area.center
    target = (round(center[0]+dir[0]*size), round(center[1]+dir[1]*size))
    pygame.draw.rect(display_surf, (0,0,0), area, 0)  # fill with black
    pygame.draw.circle(display_surf, (255,255,255), center, size, 0)
    pygame.draw.line(display_surf, color, center, target, 1)
    pygame.draw.circle(display_surf, color, target, 4, 0)

def display_response(response, display_surf, camera_outputs, print_observation=False, write_video=False):
    global VIDEO_WRITER
    observation = response.get('observation')
    sensor_data = observation.get('sensors')
    measurements = observation.get('measurements')

    def printable(x): return type(x) is not bytearray and type(x) is not np.ndarray
    if observation is not None and print_observation:
        simple_observations = {k: v for k, v in observation.items() if k not in ['measurements', 'sensors']}
        dicts = [simple_observations, observation.get('measurements'), observation.get('sensors')]
        for d in dicts:
            for k, v in d.items():
                if type(v) is not dict:
                    info = '%s: %s' % (k,v)
                    print(info[:75] + (info[75:] and '..'))
                else:
                    print('%s: %s' % (k, str({i: v[i] for i in v if printable(v[i])})))
        if 'forces' in sensor_data:
            print('forces: %s' % str(sensor_data['forces']['data']))
        if 'info' in response:
            print('info: %s' % str(response['info']))

    if 'offset' in camera_outputs:
        draw_offset(measurements.get('offset_to_goal'), display_surf, camera_outputs['offset']['area'])

    for obs, config in camera_outputs.items():
        if obs not in sensor_data:
            continue
        if obs == 'forces':
            draw_forces(sensor_data['forces']['data'], display_surf, config['area'])
            continue
        img = sensor_data[obs]['data']
        img_viz = sensor_data[obs].get('data_viz')
        if obs == 'depth':
            img *= (255.0 / img.max())  # naive rescaling for visualization
            img = img.astype(np.uint8)
        elif img_viz is not None:
            img = img_viz
        blit_img_to_surf(img, display_surf, config.get('position'))

        # TODO: consider support for writing to video of all camera modalities together
        if obs == 'color':
            if write_video and VIDEO_WRITER:
                if len(img.shape) == 2:
                    VIDEO_WRITER.add_frame(np.dstack([img, img, img]))  # yyy
                else:
                    VIDEO_WRITER.add_frame(img[:, :, :-1])  # rgb

    if 'audio' in sensor_data:
        audio_data = sensor_data['audio']['data']
        pygame.sndarray.make_sound(audio_data).play()
        # pygame.mixer.Sound(audio_data).play()

def ensure_size(display_surf, rw, rh):
    w = display_surf.get_width()
    h = display_surf.get_height()
    if w < rw or h < rh:
        # Resize display (copying old stuff over)
        old_display_surf = display_surf.convert()
        display_surf = pygame.display.set_mode((max(rw,w), max(rh,h)), pygame.RESIZABLE | pygame.DOUBLEBUF)
        display_surf.blit(old_display_surf, (0,0))
        return display_surf, True
    else:
        return display_surf, False

def write_text(display_surf, text, position, font=None, fontname='monospace', fontsize=12, color=(255,255,224), align=None):
    """
    text -> string of text.
    fontname-> string having the name of the font.
    fontsize -> int, size of the font.
    color -> tuple, adhering to the color format in pygame.
    position -> tuple (x,y) coordinate of text object.
    """

    font_object = font if font is not None else pygame.font.SysFont(fontname, fontsize)
    text_surface = font_object.render(text, True, color)
    if align is not None:
        text_rectangle = text_surface.get_rect()
        if align == 'center':
            text_rectangle.center = position[0], position[1]
        else:
            text_rectangle.topleft = position
        display_surf.blit(text_surface, text_rectangle)
    else:
        display_surf.blit(text_surface, position)

def calculate_nine_region_bounds(start_position, end_position):
    x_distance=end_position[0]-start_position[0]
    x_distance_diff=x_distance/3
    z_distance=end_position[2]-start_position[2]
    z_distance_diff=z_distance/3

    bounds=[]
    row_0=[]
    row_1=[]
    row_2=[]
    row_0.append(
        {
            'start_x':start_position[0],
            'start_y':start_position[1],
            'start_z':start_position[2],
        
            'end_x':(start_position[0]+x_distance_diff) if end_position[0]>start_position[0] else (start_position[0]-x_distance_diff),
            'end_y':start_position[1],
            'end_z':(start_position[2]+z_distance_diff) if end_position[2]>start_position[2] else (start_position[2]-z_distance_diff)
        }
    )
    row_0.append(
        {
            'start_x':(start_position[0]+x_distance_diff) if end_position[0]>start_position[0] else (start_position[0]-x_distance_diff),
            'start_y':start_position[1],
            'start_z':start_position[2],
            
            'end_x':(start_position[0]+2*x_distance_diff) if end_position[0]>start_position[0] else (start_position[0]-2*x_distance_diff),
            'end_y':start_position[1],
            'end_z':(start_position[2]+z_distance_diff) if end_position[2]>start_position[2] else (start_position[2]-z_distance_diff)
        }
    )
    row_0.append(
        {
            'start_x':(start_position[0]+2*x_distance_diff) if end_position[0]>start_position[0] else (start_position[0]-2*x_distance_diff),
            'start_y':start_position[1],
            'start_z':start_position[2],
            
            'end_x':end_position[0],
            'end_y':start_position[1],
            'end_z':(start_position[2]+z_distance_diff) if end_position[2]>start_position[2] else (start_position[2]-z_distance_diff)
        }
    )
    row_1.append(
        {
            'start_x':start_position[0],
            'start_y':start_position[1],
            'start_z':(start_position[2]+z_distance_diff) if end_position[2]>start_position[2] else (start_position[2]-z_distance_diff),

            'end_x':(start_position[0]+x_distance_diff) if end_position[0]>start_position[0] else (start_position[0]-x_distance_diff),
            'end_y':start_position[1],
            'end_z':(start_position[2]+2*z_distance_diff) if end_position[2]>start_position[2] else (start_position[2]-2*z_distance_diff)
        }
    )
    row_1.append(
        {
            'start_x':(start_position[0]+x_distance_diff) if end_position[0]>start_position[0] else (start_position[0]-x_distance_diff),
            'start_y':start_position[1],
            'start_z':(start_position[2]+z_distance_diff) if end_position[2]>start_position[2] else (start_position[2]-z_distance_diff),
            
            'end_x':(start_position[0]+2*x_distance_diff) if end_position[0]>start_position[0] else (start_position[0]-2*x_distance_diff),
            'end_y':start_position[1],
            'end_z':(start_position[2]+2*z_distance_diff) if end_position[2]>start_position[2] else (start_position[2]-2*z_distance_diff)
        }
    )
    row_1.append(
        {
            'start_x':(start_position[0]+2*x_distance_diff) if end_position[0]>start_position[0] else (start_position[0]-2*x_distance_diff),
            'start_y':start_position[1],
            'start_z':(start_position[2]+z_distance_diff) if end_position[2]>start_position[2] else (start_position[2]-z_distance_diff),

            'end_x':end_position[0],
            'end_y':start_position[1],
            'end_z':(start_position[2]+2*z_distance_diff) if end_position[2]>start_position[2] else (start_position[2]-2*z_distance_diff)
        }
    )
    row_2.append(
        {
            'start_x':start_position[0],
            'start_y':start_position[1],
            'start_z':(start_position[2]+2*z_distance_diff) if end_position[2]>start_position[2] else (start_position[2]-2*z_distance_diff),
            
            'end_x':(start_position[0]+x_distance_diff) if end_position[0]>start_position[0] else (start_position[0]-x_distance_diff),
            'end_y':start_position[1],
            'end_z':end_position[2]
        }
    )
    row_2.append(
        {
            'start_x':(start_position[0]+x_distance_diff) if end_position[0]>start_position[0] else (start_position[0]-x_distance_diff),
            'start_y':start_position[1],
            'start_z':(start_position[2]+2*z_distance_diff) if end_position[2]>start_position[2] else (start_position[2]-2*z_distance_diff),
            
            'end_x':(start_position[0]+2*x_distance_diff) if end_position[0]>start_position[0] else (start_position[0]-2*x_distance_diff),
            'end_y':start_position[1],
            'end_z':end_position[2]
        }
    )
    row_2.append(
        {
            'start_x':(start_position[0]+2*x_distance_diff) if end_position[0]>start_position[0] else (start_position[0]-2*x_distance_diff),
            'start_y':start_position[1],
            'start_z':(start_position[2]+2*z_distance_diff) if end_position[2]>start_position[2] else (start_position[2]-2*z_distance_diff),
            
            'end_x':end_position[0],
            'end_y':start_position[1],
            'end_z':end_position[2]
        }
    )
    bounds.append(row_0)
    bounds.append(row_1)
    bounds.append(row_2)
    return bounds

def is_within_region(position,region_bounds):
    if position[0]>=min(region_bounds['start_x'],region_bounds['end_x']) and position[2]>=min(region_bounds['start_z'],region_bounds['end_z']) and position[0]<=max(region_bounds['start_x'],region_bounds['end_x']) and position[2]<=max(region_bounds['start_z'],region_bounds['end_z']):
        return True
    else:
        return False

def get_region(position,bounds):
    if is_within_region(position,bounds[0][0]):
        return 1
    if is_within_region(position,bounds[0][1]):
        return 2
    if is_within_region(position,bounds[0][2]):
        return 3
    if is_within_region(position,bounds[1][0]):
        return 4
    if is_within_region(position,bounds[1][1]):
        return 5
    if is_within_region(position,bounds[1][2]):
        return 6
    if is_within_region(position,bounds[2][0]):
        return 7
    if is_within_region(position,bounds[2][1]):
        return 8
    if is_within_region(position,bounds[2][2]):
        return 9

def get_random_action(rotation=True):
    # 119:Forward
    # 115: Backward
    # 97: Left
    # 100: Right
    # 276: CCW
    # 275: CW

    actions = [119, 115, 97, 100, 276, 275]
    try:
        if rotation:
            rand_no = random.randint(0, 5)
        else:
            rand_no = random.randint(0, 3)
        next_action = actions[rand_no]
    except IndexError:
        if rotation:
            next_action = actions[5]
        else:
            next_action = actions[3]
    return next_action

previous_action = 0
next_action = 0
rotation_counter=0
reverse_action_taken =False

def reverse_action(previous_action):
    if previous_action==119:
        return 115
    elif previous_action==115:
        return 119
    elif previous_action==97:
        return 100
    elif previous_action==100:
        return 97
    elif previous_action==276:
        return 275
    elif previous_action==275:
        return 276
    return None

def generate_key_press_random(has_collided, rotation=True, confine_to_room='', current_room='', room_bounds={}, current_position=[0,0,0] ):
    global previous_action, next_action, rotation_counter, reverse_action_taken
    time.sleep(0.5)
    previous_action = next_action
    if confine_to_room!='' and current_room!=confine_to_room:
        if not reverse_action_taken:
            print(f'Exited Room: {confine_to_room}, Current Room: {current_room}, Reversing Last Action')
            next_action = reverse_action(previous_action=previous_action)
            reverse_action_taken = True
        else:
            print(f'Havent reached back to Room{confine_to_room}, Current Room: {current_room}, Continuing Reverse Action')
            next_action = previous_action
            reverse_action_taken =True
    elif room_bounds and current_position!=[0,0,0] and not is_within_region(position=current_position,region_bounds=room_bounds):
        if not reverse_action_taken:
            print(f'Exited Region: {room_bounds}, Current Position: {current_position}, Reversing Last Action')
            next_action = reverse_action(previous_action=previous_action)
            reverse_action_taken = True
        else:
            print(f'Havent reached back to Region{room_bounds}, Current Position: {current_position}, Continuing Reverse Action')
            next_action = previous_action
            reverse_action_taken =True

    elif has_collided:
        print('Collision Detected: Taking Random Action')
        next_action = get_random_action(rotation=rotation)
        reverse_action_taken = False
    else:
        if previous_action==0:
            next_action = 119
        else:
            if (previous_action==276 and rotation_counter%2==0) or (previous_action==275 and rotation_counter%2==0):
                rotation_counter=0
                next_action=get_random_action(rotation=rotation)
            else:
                print('No Collision: Moving Continuing Previous Action')
                next_action = previous_action
        reverse_action_taken = False
    
    if next_action== 275 or next_action==276:
        rotation_counter=rotation_counter+1
    empty_keys = np.zeros(323, dtype='i')
    empty_keys[next_action] = 1
    return tuple(empty_keys)

def interactive_loop(sim, args):
    # initialize
    pygame.mixer.pre_init(frequency=8000, channels=1)
    pygame.init()
    pygame.key.set_repeat(500, 50)  # delay, interval
    clock = pygame.time.Clock()

    # Set up display
    all_camera_observations = ['color', 'depth', 'normal', 'objectId', 'objectType', 'roomId', 'roomType']
    label_positions = {
        'curr': {},
        'goal': {}
    }
    camera_outputs = {
        'curr': {},
        'goal': {}
    }

    # get observation space and max height
    observation_space = sim.get_observation_space()
    spaces = [observation_space.get('sensors').get(obs) for obs in all_camera_observations if args.observations.get(obs)]
    heights = [s.shape[1] for s in spaces]

    # row with observations and goals
    nimages = 0
    total_width = 0
    max_height = max(heights)
    font_spacing = 20
    display_height = max_height + font_spacing*3
    for obs in all_camera_observations:
        if args.observations.get(obs):
            space = observation_space.get('sensors').get(obs)
            print('space', space)
            width = space.shape[0]   # TODO: have height be first to be more similar to other libraries
            height = space.shape[1]
            label_positions['curr'][obs] = (total_width, font_spacing*2)
            camera_outputs['curr'][obs] = { 'position': (total_width, font_spacing*3) }
            if args.show_goals:
                label_positions['goal'][obs] = (total_width, display_height + font_spacing*2)
                camera_outputs['goal'][obs] = { 'position': (total_width, display_height + font_spacing*3) }
            nimages += 1
            total_width += width
            if height > max_height:
                max_height = height


    if args.show_goals:
        display_height += max_height + font_spacing*3

    # Row with offset and map
    plot_size = max(min(args.height, 128), 64)
    display_height += font_spacing
    label_positions['curr']['offset'] = (0, display_height)
    camera_outputs['curr']['offset'] = { 'area': pygame.Rect(0, display_height + font_spacing, plot_size, plot_size)}

    next_start_x = plot_size
    if args.observations.get('forces'):
        label_positions['curr']['forces'] = (next_start_x, display_height)
        camera_outputs['curr']['forces'] = { 'area': pygame.Rect(next_start_x, display_height + font_spacing, plot_size, plot_size)}
        next_start_x += plot_size

    if args.observations.get('map'):
        label_positions['map'] = (next_start_x, display_height)
        camera_outputs['map'] = { 'position': (next_start_x, display_height + font_spacing) }

    display_height += font_spacing
    display_height += plot_size

    display_shape = [max(total_width, next_start_x), display_height]
    display_surf = pygame.display.set_mode((display_shape[0], display_shape[1]), pygame.RESIZABLE | pygame.DOUBLEBUF)

    # Write text
    label_positions['title'] = (display_shape[0]/2, font_spacing/2)
    write_text(display_surf, 'MINOS', fontsize=20, position = label_positions['title'], align='center')
    write_text(display_surf, 'dir_to_goal', position = label_positions['curr']['offset'])
    if args.observations.get('forces'):
        write_text(display_surf, 'forces', position = label_positions['curr']['forces'])
    if args.observations.get('map'):
        write_text(display_surf, 'map', position = label_positions['map'])
    write_text(display_surf, 'observations | controls: WASD+Arrows', position = (0, font_spacing))
    if args.show_goals:
        write_text(display_surf, 'goal', position = (0, args.height + font_spacing*3 + font_spacing))
    for obs in all_camera_observations:
        if args.observations.get(obs):
            write_text(display_surf, obs, position = label_positions['curr'][obs])
            if args.show_goals:
                write_text(display_surf, obs, position = label_positions['goal'][obs])

    # Other initialization
    scene_index = 0
    scene_dataset = args.scene.dataset

    init_time = timer()
    num_frames = 0
    prev_key = ''
    replay = args.replay
    action_traces = args.action_traces
    action_trace = action_traces.curr_trace() if action_traces is not None else None
    replay_auto = False
    replay_mode = args.replay_mode
    replay_mode_index = REPLAY_MODES.index(replay_mode)
    print('***\n***')
    print('CONTROLS: WASD+Arrows = move agent, R = respawn, N = next state/scene, O = print observation, Q = quit')
    if replay:
        print('P = toggle auto replay, E = toggle replay using %s '
              % str([m + ('*' if m == replay_mode else '') for m in REPLAY_MODES]))
    print('***\n***')

    ##########################################################
    
    has_collided=False
    direction=[0.0,0.0,0.0]
    distance=10
    position_internal=[0.0,0.0,0.0]
    
    filename='positions.csv'
    secs_per_room=20
    positions_df=pd.read_csv(filename)


    for index,row in positions_df.iterrows():
        
        current_room=row['room_id']
        start_position=[row['start_position_0'],row['start_position_1'],row['start_position_2']]
        end_position=[row['end_position_0'],row['end_position_1'],row['end_position_2']]
        start_angle=row['start_angle']
        print('Setting Agent Position:')
        print('Position: ', start_position)
        print('Angle: ', start_angle)
        sim.move_to(pos= start_position,angle=start_angle )

        bounds= calculate_nine_region_bounds(
            start_position=start_position,
            end_position=end_position
        )
        print('Nine Region Bounds:',bounds)
        #region=get_region(position=end_position,bounds=bounds)
        room_init_time= timer()
        
    ##########################################################
        while sim.running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    sim.running = False

            if timer()-room_init_time>secs_per_room:
                print(f'Exploration Done in Room {current_room}')
                break
            # read keys
            #keys = pygame.key.get_pressed()
            room_bounds= {'start_x':start_position[0],'start_y':start_position[1],'start_z':start_position[2], 'end_x':end_position[0], 'end_y':end_position[1], 'end_z':end_position[2]}
            keys = generate_key_press_random(has_collided,rotation=False,confine_to_room=current_room,current_room=current_room, room_bounds={},current_position=position_internal)
            #print(type(keys),len(keys))
            print_next_observation = False
            if keys[K_q]:
                break

            ##################################
            if keys[K_x]:
                print('In Room: ',current_room)
                if Path(filename).is_file():
                    print('Positions File Exists')
                    df = pd.read_csv(filename)
                    if not df.loc[(df['scene_id']==args['scene_ids'][0]) & (df['room_id']==current_room)].empty:
                        df.loc[(df['scene_id']==args['scene_ids'][0]) & (df['room_id']==current_room),'start_position_0']=position_internal[0]
                        df.loc[(df['scene_id']==args['scene_ids'][0]) & (df['room_id']==current_room),'start_position_1']=position_internal[1]
                        df.loc[(df['scene_id']==args['scene_ids'][0]) & (df['room_id']==current_room),'start_position_2']=position_internal[2]
                        df.loc[(df['scene_id']==args['scene_ids'][0]) & (df['room_id']==current_room),'start_angle']=angle_internal
                        df.to_csv(filename, index=False)
                    else:
                        position_data= {
                            'scene_id': args['scene_ids'][0],
                            'room_id':current_room,
                            'start_position_0': [position_internal[0]],
                            'start_position_1': [position_internal[1]],
                            'start_position_2': [position_internal[2]],
                            'start_angle': [angle_internal]
                        }
                        df2=pd.DataFrame(position_data)
                        df=df.append(df2,ignore_index=True)
                        df.to_csv(filename, index=False)
                else:
                    print('Positions File does not exist')
                    position_data= {
                        'scene_id': args['scene_ids'][0],
                        'room_id':current_room,
                        'start_position_0': [position_internal[0]],
                        'start_position_1': [position_internal[1]],
                        'start_position_2': [position_internal[2]],
                        'start_angle': [angle_internal]
                    }
                    df=pd.DataFrame(position_data)
                    df.to_csv(filename, index=False)
            
            if keys[K_c]:
                print('In Room: ',current_room)
                if Path(filename).is_file():
                    print('Positions File Exists')
                    df = pd.read_csv(filename)
                    if not df.loc[(df['scene_id']==args['scene_ids'][0]) & (df['room_id']==current_room)].empty:
                        df.loc[(df['scene_id']==args['scene_ids'][0]) & (df['room_id']==current_room),'end_position_0']=position_internal[0]
                        df.loc[(df['scene_id']==args['scene_ids'][0]) & (df['room_id']==current_room),'end_position_1']=position_internal[1]
                        df.loc[(df['scene_id']==args['scene_ids'][0]) & (df['room_id']==current_room),'end_position_2']=position_internal[2]
                        df.loc[(df['scene_id']==args['scene_ids'][0]) & (df['room_id']==current_room),'end_angle']=angle_internal
                        df.to_csv(filename, index=False)
                    else:
                        position_data= {
                            'scene_id': args['scene_ids'][0],
                            'room_id':current_room,
                            'start_position_0': [position_internal[0]],
                            'start_position_1': [position_internal[1]],
                            'start_position_2': [position_internal[2]],
                            'start_angle': [angle_internal]
                        }
                        df2=pd.DataFrame(position_data)
                        df=df.append(df2,ignore_index=True)
                        df.to_csv(filename, index=False)
                else:
                    print('Positions File does not exist')
                    position_data= {
                        'scene_id': args['scene_ids'][0],
                        'room_id':current_room,
                        'end_position_0': [position_internal[0]],
                        'end_position_1': [position_internal[1]],
                        'end_position_2': [position_internal[2]],
                        'end_angle': [angle_internal]
                    }
                    df=pd.DataFrame(position_data)
                    df.to_csv(filename, index=False)
            ##################################
            if keys[K_o]:
                print_next_observation = True
            elif keys[K_n]:
                prev_key = 'n' if prev_key is not 'n' else ''
                if 'state_set' in args and prev_key is 'n':
                    state = args.state_set.get_next_state()
                    if not state:  # roll over to beginning
                        print('Restarting from beginning of states file...')
                        state = args.state_set.get_next_state()
                    id = scene_dataset + '.' + state['scene_id']
                    print('next_scene loading %s ...' % id)
                    sim.set_scene(id)
                    sim.move_to(state['start']['position'], state['start']['angle'])
                    sim.episode_info = sim.start()
                elif prev_key is 'n':
                    scene_index = (scene_index + 1) % len(args.scene_ids)
                    scene_id = args.scene_ids[scene_index]
                    id = scene_dataset + '.' + scene_id
                    print('next_scene loading %s ...' % id)
                    sim.set_scene(id)
                    sim.episode_info = sim.start()
            elif keys[K_r]:
                prev_key = 'r' if prev_key is not 'r' else ''
                if prev_key is 'r':
                    sim.episode_info = sim.reset()
            else:
                # Figure out action
                action = {'name': 'idle', 'strength': 1, 'angle': math.radians(5)}
                actions = []
                if replay:
                    unprocessed_keypressed = any(keys)
                    if keys[K_p]:
                        prev_key = 'p' if prev_key is not 'p' else ''
                        if prev_key == 'p':
                            replay_auto = not replay_auto
                            unprocessed_keypressed = False
                    elif keys[K_e]:
                        prev_key = 'e' if prev_key is not 'e' else ''
                        if prev_key == 'e':
                            replay_mode_index = (replay_mode_index + 1) % len(REPLAY_MODES)
                            replay_mode = REPLAY_MODES[replay_mode_index]
                            unprocessed_keypressed = False
                            print('Replay using %s' % replay_mode)

                    if replay_auto or unprocessed_keypressed:
                        # get next action and do it
                        rec = action_trace.next_action_record()
                        if rec is None:
                            # go to next trace
                            action_trace = action_traces.next_trace()
                            start_state = action_trace.start_state()
                            print('start_state', start_state)
                            sim.configure(start_state)
                            sim.episode_info = sim.start()
                        else:
                            if replay_mode == 'actions':
                                actnames = rec['actions'].split('+')
                                for actname in actnames:
                                    if actname != 'reset':
                                        act = copy.copy(action)
                                        act['name'] = actname
                                        actions.append(act)
                            elif replay_mode == 'positions':
                                sim.move_to([rec['px'], rec['py'], rec['pz']], rec['rotation'])
                else:
                    if keys[K_w]:
                        action['name'] = 'forwards'
                        print('Moving Forward')
                    elif keys[K_s]:
                        action['name'] = 'backwards'
                        print('Moving Backward')
                    elif keys[K_LEFT]:
                        action['name'] = 'turnLeft'
                        print('Rotating CCW')
                    elif keys[K_RIGHT]:
                        action['name'] = 'turnRight'
                        print('Rotating CW')
                    elif keys[K_a]:
                        action['name'] = 'strafeLeft'
                        print('Moving Left')
                    elif keys[K_d]:
                        action['name'] = 'strafeRight'
                        print('Moving Right')
                    elif keys[K_UP]:
                        action['name'] = 'lookUp'
                    elif keys[K_DOWN]:
                        action['name'] = 'lookDown'
                    else:
                        action['name'] = 'idle'
                    actions = [action]

            # step simulator and get observation
            response = sim.step(actions, 1)
            if response is None:
                break

            display_episode_info(sim.episode_info, display_surf, camera_outputs['goal'], show_goals=args.show_goals)

            # Handle map
            observation = response.get('observation')
            
            # if not position==[] and not orientation_angle==[]:
            #     print('Restoring Saved Position: ',position, orientation_angle)
            #     sim.move_to(position,angle=orientation_angle,tilt=0)

            position_internal = response['info']['agent_state']['position']
            orientation_internal = response['info']['agent_state']['orientation']
            angle_internal = get_angle_rad(orientation_internal[2], orientation_internal[0])
            current_room=observation['roomInfo']['id']
            has_collided = observation['collision']
            direction = observation['measurements']['direction_to_goal']
            # print('Position: ',response['info']['agent_state']['position'])
            # print('Orientation: ',response['info']['agent_state']['orientation'])
            ###########################
            map = observation.get('map')
            if map is not None:
                # TODO: handle multiple maps
                if isinstance(map, list):
                    map = map[0]
                config = camera_outputs['map']
                img = map['data']
                rw = map['shape'][0] + config.get('position')[0]
                rh = map['shape'][1] + config.get('position')[1]
                display_surf, resized = ensure_size(display_surf, rw, rh)
                if resized:
                    write_text(display_surf, 'map', position = label_positions['map'])
                blit_img_to_surf(img, display_surf, config.get('position'), surf_key='map')

            # Handle other response
            display_response(response, display_surf, camera_outputs['curr'], print_observation=print_next_observation, write_video=True)
            pygame.display.flip()
            num_frames += 1
            clock.tick(30)  # constraint to max 30 fps

    # NOTE: log_action_trace handled by javascript side
    # if args.log_action_trace:
    #     trace = sim.get_action_trace()
    #     print(trace['data'])

    # cleanup and quit
    time_taken = timer() - init_time
    print('time=%f sec, fps=%f' % (time_taken, num_frames / time_taken))
    print('Thank you for playing - Goodbye!')
    pygame.quit()


def main():
    global VIDEO_WRITER
    parser = argparse.ArgumentParser(description='Interactive interface to Simulator')
    parser.add_argument('--navmap', action='store_true',
                        default=False,
                        help='Use navigation map')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--state_set_file',
                       help='State set file')
    group.add_argument('--replay',
                       help='Load and replay action trace from file')
    group.add_argument('--replay_mode',
                       choices=REPLAY_MODES,
                       default='positions',
                       help='Use actions or positions for replay')
    group.add_argument('--show_goals', action='store_true',
                       default=False,
                       help='show goal observations')

    args = parse_sim_args(parser)
    args.visualize_sensors = True
    sim = Simulator(vars(args))
    common.attach_exit_handler(sim)

    if 'state_set_file' in args and args.state_set_file is not None:
        args.state_set = StateSet(args.state_set_file, 1)
    if 'save_video' in args and len(args.save_video):
        filename = args.save_video if type(args.save_video) is str else 'out.mp4'
        is_rgb = args.color_encoding == 'rgba'
        VIDEO_WRITER = VideoWriter(filename, framerate=24, resolution=(args.width, args.height), rgb=is_rgb)
    if 'replay' in args and args.replay is not None:
        print('Initializing simulator using action traces %s...' % args.replay)
        args.action_traces = ActionTraces(args.replay)
        action_trace = args.action_traces.next_trace()
        sim.init()
        start_state = action_trace.start_state()
        print('start_state', start_state)
        sim.configure(start_state)
    else:
        args.action_traces = None
        args.replay = None

    try:
        print('Starting simulator...')
        ep_info = sim.start()
        if ep_info:
            print('observation_space', sim.get_observation_space())
            sim.episode_info = ep_info
            print('Simulator started.')
            interactive_loop(sim, args)
    except:
        traceback.print_exc()
        print('Error running simulator. Aborting.')

    if sim is not None:
        sim.kill()
        del sim

    if VIDEO_WRITER is not None:
        VIDEO_WRITER.close()


if __name__ == "__main__":
    main()
