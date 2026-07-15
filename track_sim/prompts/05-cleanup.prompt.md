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


## AI tuning

remove the logic that makes the leader stop adjusting waypoints

increase the amount of lateral line offset that can be learned by the AI. currently it is too small and the cars are all driving the same line.


## Tracksim flow changes

infinite loop. When all cars wreck, reset to new race

re-read car names(this way I can SSH in, vim the file with new names and they'll show up next race)