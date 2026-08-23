# bug fixes and new features

discovered some bugs, and need to push in new features. 

## bugs

found bugs

### waypoint density too low

I have to hit the `+ waypoint` button 20 times. Make that the default density

### race stats window showing up twice

leftover window element

keep the one that is in the stats panel, remove the other one

rewire the `Toggle Race Stats` button to toggle the race stats 1 panel

### car stats window showing up twice

leftover window element

keep the one that is in the stats panel, remove the other one

rewire the `Toggle Car Stats` button to toggle the car stats panel

for the car stats panel, have a dropdown menu to select which car to display stats for. The default will be the first car in the list. 

### clicking elsewhere does not deselect a car

make it so that clicking anywhere else on the track will deselect. This works while the simulation is not running but not while it is running. Make it work while the simulation is running as well.

## features

### remove keyboard driving

remove manual mode toggle, 

remove visual "AUTO (A)" indicator

remove keyboard inputs for driving

cars will always be in auto mode, and will always be driven by the AI

### give each car their own unique starting waypoints 

based on their starting positions on the track. Each car should have a unique set of waypoints to lay out their most efficient path around the track.

this is to fix the regression issue where most cars crash in the first turn because they are all trying to follow the same path. Cars in the back are less likely to complete laps because of the congestion in the first turn.

they will update their waypoints as they drive to optimize their path around track, including avoiding cars, wrecks and flying off track. 

### add a configurable lap limit to series races

add this this to the tracksim.conf file

When the last unwrecked car completes the defined number of laps, the race is complete. Series scoring is based on the on the fastest time a car completes the defined number of laps. 5 points for first, 4 for second, 3 for third, 2 for fourth, 1 for completing the race. The points will be accumulated across the races and the series winner will be the car with the most points at the end of the series.