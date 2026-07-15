# car vision implementation

Replaces existing car's system of planning movements around the track with waypoints and vision-based navigation.

## car waypointing

Waypoints are looping ordered list of coordinates the car should follow around the track. They are connected by straight lines. These lines cannot go outside of the racing surface boundary

Upon loading the race map, each car should place their waypoints in the most efficient route possible. Efficiency criteria: least amount of waypoints used, doesn't go off the racing surface boundary, and follows the natural flow of the track. 

When the race starts, they will attempt to follow the waypoints but also update them as needed using input from their vision matrix. 

These waypoints will be saved in the map for each car loaded and can be reused in future races.

## car vision

Cars have a 70 degree forward vision cone the length equilavent to 5x the length of the car. This cone determines what the car can see in front of it and is used to update the vision matrix continuously as the car moves.

Represented by a matrix (x, y) where x is left, center or right and where y is near, middle or far
Each cell in the matrix represents the empty, waypoints or obstacles (wreck, car, barrier) in that direction and distance.

states:
- clear
- waypoint
- wreck
- car
- barrier


## car logic

Cars priorities:

1. accelerate from waypoint to waypoint. 
2. avoid obstacles whenever possible.
3. They prefer turning over coasting.
4. they prefer coasting over drifting.
5. They prefer drifting over braking.
6. They prefer braking over reversing.

Cars want to align to waypoints(turn the car towards the waypoint until it is the center of the vision matrix without any obstacles in the way) in order to follow the most efficient route around the track.

If a car is taking damage, it will lay a temporary waypoint to move it away and forward from the source of the damage.

If a car can't see a waypoint, it will lay a temporary waypoint along the car's current heading, biased toward the last known bearing to the next permanent waypoint.

If a car's path to the next waypoint is blocked by an obstacle, it will lay temporary waypoint in the direction it believes is the best route to navigate around the obstacle to the next waypoint without colliding with obstacles.

The temporary waypoint will get promoted to a permanent waypoint if the lap time of the lap it was placed improves upon the car's current best lap time, otherwise it will be discarded.

Cars want to avoid obstacles in order to maintain a clear path to the next waypoint.

Cars should continuously update their vision matrix as they move, to react to dynamic changes in the environment.

In the track simulator, when selecting a car, it will show their vision cone, their current waypoints(circle), and any temporary waypoints(dotted line circle) they have laid down around the track.


## track simulator improvements

Selecting a car will show its vision cone, current waypoints, and any temporary waypoints it has laid down around the track.

In the menu bar there will be a drop down menu called stats with a visibiliy toggle for each of the race stats, debug information and car stats panes.

A race stats pane will display next to the debug pane showing the current number of laps completed, best lap time and the car that currently holds the best lap time. It will also show the worst lap time and the car that currently holds the worst lap time.

