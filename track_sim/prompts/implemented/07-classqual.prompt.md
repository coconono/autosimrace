# Class Qualification

in tracksim replace infinite mode with session mode, update the dropdown menu entry

types of cars
- cars are defined under the /tracksim/cars/ directory
- cars are instanced into maps in the /tracksim/maps/ directory
- EG: CloverFPV is a type of car called "car", car2_2 is a type of car called "car2"


in session mode
- each type of car will have a series of qualifying races(defined in tracksim.conf under series configuration)
- they will start from their positions defined on the map
- winner of the race goes to the back of the grid
- the rest of the cars move up one position
- repeat for each type of car
- once all types of cars have had their qualifying races, the main series will begin with the cars in their updated positions
- series points are accrued during the qualifying races 


series stats pane:
- add line to display if qualifying race or main series race
- when qualifying race show type of car qualifying
- when qualifying race show the number of the qualifying race
- when main series race show the current race number within the main series