# build a track and car simulation

first pass implementation of a track generator and car simulation

for now we are going to focus on generating the track layout and simulating car movement on it with a single car. later versions will focus on multiple cars so we need to design the track and car simulation with scalability in mind.

## definitions
a car is represented as a rectangle moving either on the inside or the outside of the track
![car](images/car.drawio.png)

the track is represented as a closed loop, an outer barrier and an inner barrier.

The outer barrier is made up of a series of lines(called short or long straights) and curves that are enclosed to form a continuous loop.
![curves](images/curve.drawio.png)
![long_straight](images/long_straight.drawio.png)
![short_straight](images/short_straight.drawio.png)

these can be rotated on 90 degree angles

The inner barrier will also be made up of a series of lines(called short or long straights) and curves that are enclosed to form a continuous loop that matches out the outer barrier but seperate enough to allow cars to pass side by side. 

![curve](images/curve.drawio.png)
![short_barrier](images/short_barrier.drawio.png)
![long_barrier](images/long_barrier.drawio.png)

these barriers are objects with fixed lengths and cannot be stretched or compressed.

barriers must touch two other barriers of their type(outer, inner).

barriers cannot overlap with each other.

the racing surface is defined by the area enclosed between the inner and outer barriers.

one area of the racing surface next to a short inner barrier must be defined as the starting grid.

entry into this area constitutes a lap only if you have left the area previously. this ensures that a lap is only counted when the car completes a full circuit of the track.

a lap is a counter-clockwise traversal of the track that starts and ends at the starting grid area.

## car
cars are physical objects that obey the laws of motion, including forces, acceleration, and collisions.

![car](images/car.drawio.png)

cars have a length and a width.
cars have a front and a back
cars have mass
cars have a maximum speed.
cars have tire health.
cars have fuel.
cars have damage.
cars have a current speed.
cars have a current direction.

cars have several states
- stopped
- braking
- moving forward
- turning left
- turning right
- reversing
- drifting
- crashed 

stopped is a state where the car is not moving and is at rest. this is used for starting or finishing the race.

braking is a state where the car is actively slowing down by applying the brakes.

moving forward is a state where the car is accelerating or maintaining its forward motion.

turning left is a state where the car is steering to the left while moving.

turning right is a state where the car is steering to the right while moving.

reversing is a state where the car is moving backward.

drifting is a state where the car is sliding sideways while maintaining control.

crashed is a special state, it indicates that the car has collided with an obstacle or another car and can no longer move until it is reset. a flame effect will appear:
![flame_effect_car](images/flame_effect_car.png)

if 3 or more cars are nearby and in a crashed state the will all get the larger flame effect.
![flame_effect](images/large_flame_effect_car.png)

car behavior network
- a car wants to finish a lap as fast as possible.
- a car does not want to hit anything, including other cars, the inner and outer barriers.
- a car network will take inputs information about the section of the track its on and the next section of the track layout, proximity to other cars, proximity to barriers and the car's current state.
- a car network will output the car's next action, such as accelerating, braking, or turning.
- a car will have a top speed that it cannot exceed.
- a car will have tire health that affects its grip and handling on the racing surface. it slowly dgrades over time. 
- tire grip is derived from the tire health and affects the car's ability to maintain control and speed, especially during turns and drifts.
- a car will have fuel that depletes over time and affects its performance.
- a car will have damage that affects its performance and handling.
- when a car's tire health, fuel, or damage reaches critical levels, it will enter a crashed state

## racing surface

the area between the inner and outer barriers where the car can drive. This will get filled in a solid black color, where the areas outside the barriers will be filled solid green.

it will have a friction coefficient that affects the car's movement and speed on the track.

if a car loses grip on the racing surface, it will drift, affecting its control and speed.


## programming notes

need to create car editor, track generator and race simulation.

use the existing python virtual environment and import libraries as needed, create a local readme.md file to document the setup and usage instructions. maintain the requirements.txt file to keep track of dependencies.

create reusable modules and libraries for use between the all programs in the racing simulation suite.

ensure proper documentation and comments are included in all modules and libraries to facilitate understanding and maintenance.

all programs will maintain a consistent project structure, including bin, etc, and other necessary directories.

all programs will have a run_<program_name>.sh script in the bin folder to ensure the virtual environment is set up and the program is executed correctly.

all programs will use the etc folder for configuration files and other necessary settings.

all programs will use the tracks folder to store and access track layouts.

all programs will use the cars folder to store and access car configurations.

all programs will use the images folder to store and access track shape templates.

all programs will have a subdirectory in the src folder to organize their source code files.

all programs will use a 1600x900 pygame window for display and user interactions.

all programs will use a unified dropdown menu bar across the top of the window for user interactions.
the leftmost menu is the start dropdown menu. it has save, load and quit options, this is common to all programs in the racing simulation suite. both save and load are contextual to the current program. EG the car editor will save and load car configurations, the track generator will save and load track layouts.


### car editor display
need a program to set the car configurations for the racing simulation.

cars have a fixed length and a width(constrained the length and width of the car.drawio.png)

it will need to define: 
- car name
- front and a back
- mass
- maximum speed.
- starting tire health.
- starting fuel.

these will be menu items in the car_name dropdown menu

### track generation display
will need a python program to generate a track layout.

it will need a run_trackgen.sh in the bin folder to make sure the virtual environment exists, activate it, checks dependencies are installed/up to date and installs/updates them if needed, execute the script and deactivate the virtual environment afterward.

a trackgen.conf file will be used to store configuration settings for the racing simulation, such as window size, default car parameters, and track file locations. it lives in the etc folder of track_sim. it will be read by the racing generation program at startup to configure the generation environment.

startup:
- use pygame to make a 1600x900 window for displaying the track and dropdown menus the user uses to interact with the application.
- initialize pygame and set up the display window before generating the track layout.
- under the start menu the load button will load existing tracks and save will save the current track layout(if it exists).
- show a dropdown menu called generate with a generate button(begin generating the track), reset button(resets the current track layout if it exists)

generate:
- future versions will read from a configuration file or user input to generate the track layout.
- uses the shapes to generate potential track layouts.
- starts with the outer barrier.
- selects 8 random pieces from the available outer barrier pieces.
- test random connections until result is a valid continuous loop where every piece is connected to two other pieces.
- discard invalid layouts that do not meet the criteria.
- then generates the inner barrier to match the outer barrier.
- then select the starting grid area next to a short inner barrier.
- shows the user the projected layout of the track to validate.
- repeat the process until a valid track layout is generated.
- outer barrier pieces only connect to other outer barrier pieces.
- inner barrier pieces only connect to other inner barrier pieces.
- inner barrier pieces must connect in a way that matches the outer barrier.

validate:
- dropdown menu called validate gets buttons to name, save, quit, or discard the track layout.
- name allows the user to provide a name for the track layout(this is used to create the filename for saving the track).
- quit closes the application without saving the current track layout, requires user confirmation.
- save saves to a .track file in the `tracks` folder for the racing game to load and populate with cars.
- discard discards the current track layout and astarts the process to generate a new one.
- each track piece will be represented by a data type that stores its shape, position, orientation and what other two parts are connected to it.
- the track layout will be stored as a collection of these track piece data types, allowing easy manipulation and validation of the track structure.

### racing simulation display

need a python program or script(s) to display the track layout, handle car movement, and update the visual representation accordingly.

it will need a run_tracksim.sh in the bin folder to make sure the virtual environment exists, activate it, checks dependencies are installed/up to date and installs/updates them if needed, execute the script and deactivate the virtual environment afterward.

a tracksim.conf file will be used to store configuration settings for the racing simulation, such as window size, default car parameters, and track file locations. it lives in the etc folder of track_sim. It will be read by the racing simulation program at startup to configure the simulation environment.
startup:
- use pygame to make a 1600x900 window for displaying the track and buttons the user uses to interact with the application.
- it will need to render the track layout visually, showing the position of the car on the track.
- an area showing the car's current status, including state, speed, tire health, fuel, and damage.
- a dropdown menu bar with options to start a new race, load a track, or quit the application.

start a new race:
- if not loaded, it will prompt the user to load a track(go to load a track screen) or cancel to main menu.
- it will initialize the car's position on the track.
- it will reset the car's tire health, fuel, and damage to their default values.
- it will have a button to start or quit the race.
- start will begin the racing simulation loop(see racing section)
- quit will exit the race and return to the main menu.

load a track:
- it will prompt the user to select a track file to load.
- it will read the track layout from the selected file.
- it will validate the loaded track layout to ensure it meets the required criteria.
- it will display the loaded track layout visually for the user to confirm.
- if the user confirms, it will proceed to the racing menu.
- if the user rejects, it will return to the track loading prompt.

racing:
- it should update the display in real-time as the car moves along the track.
- car movement will be defined by the car's behavior network.
- the car's tire health, fuel, and damage should be monitored and updated accordingly.
- if the car enters a crashed state, it should stop moving and display the crashed state visually.
- the racing simulation should handle collisions between cars and barriers, updating the car's damage and tire health accordingly.
- the racing simulation should account for drifting when a car loses grip, affecting its control and speed.
- the racing simulation should update the car's position and orientation based on its velocity, acceleration, and steering inputs.
- the racing simulation should provide visual feedback for the car's tire health, fuel, and damage status, allowing the user to monitor the car's condition in real-time.

