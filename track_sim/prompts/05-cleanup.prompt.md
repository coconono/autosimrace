# UI clean up and feature improvements

some outstanding stuff that needs to be done to make the track_sim more polished and ready for streaming.

## fix the tracksim Simulate mode

cars aren't completing sim races. there is a logic error where the increase of simulation speed is causing the cars to not be able to complete laps. needs fixed. 

## tracksim UI matters


text overruns
- change Increase Waypoint Density to '+ Waypoint'
- change Decrease Waypoint Density to '- Waypoint'
- variable size fonts to get the text to fit in the dropdown menus
- variable size fonts to get the text to fit in the info boxes

pane design
- separate the track into its own pane
- make a leaderboard that updates in real time, this is the side pane
- bottom pane will be stats panel, with lap times, best times, etc.

see track_sim/images/layout/RaceSimLayout.drawio.png for pane layout. 

series stats 1:
- series name
- number of races
- fastest lap of the series (car, race, lap time, lap number)
- slowest lap of the series (car, race, lap time, lap number)

series stats 2:
- top 5 points leaders

race stats 1:
- Race leader
- top speed
- laps completed
- fastest lap time
- slowest lap time

race stats 2:
- most time spent in a single drift (car name and time)
- quickest to crash (car name and time)
- last car to hit another car (car name and time)

car stats pane will remain the same other than positioning. 

## race series

tracksin.conf will need to be updated to include a series name and number of races

it will also include an entry to point to the series logo file. This will be displayed in the series stats pane.

race series are groups of races where the points are accumulated across the races. 5 points for first, 4 for second, 3 for third, 2 for fourth, 1 for completing the race. The points will be accumulated across the races and the series winner will be the car with the most points at the end of the series.

series stats pane 1 will display the series name, number of races, fastest lap of the series (car, race, lap time, lap number), slowest lap of the series (car, race, lap time, lap number).

the top 5 points leaders will be displayed in the series stats 2 pane.

### race series logo

will be displayed in the series stats pane. It will be supplied. But size requirements will need to be determined by the layout. This info should go the readme.md file. 

## AI tuning

remove the logic that makes the leader stop adjusting waypoints

increase the amount of lateral line offset that can be learned by the AI. currently it is too small and the cars aren't trying new lines.

## Tracksim flow changes

infinite loop. When all cars wreck, reset to new race

re-read car names(this way I can SSH in, vim the file with new names and they'll show up next race)