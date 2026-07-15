# copilot don't read this file

I made AI death race. They see the carnage they are driving through. They follow waypoints and try not to wreck. They learn from each turn and try to do better.

A bunch of tokens were recklessly spent on this so I gotta figure out how to recoup the loss.

I am going to make this into a streaming thing. But it needs shined up first.

game plan:

Clean up the track_sim code:

- make a leaderboard that updates in real time
- clean up UI
  - text overruns
  - seperate the track from the info boxes. seperate panes?
- infinite loop. When all cars wreck, reset to new race
  - re-read car names(this way I can SSH in, vim the file with new names and they'll show up next race)
- each car gets a number and a name. stored in the track file
- remove the logic that makes the leader not try to improve times
- unlock limits on parts for track generation
- setup colors for cars
- fix the simulator training mode since I'm pretty sure its just yeeting cars.

Setup a potato server:

- can I do this on a pi4 with a busted HDMI output?
- runs python race project on loop
- headless obs that streams 24/7 to youtube
- admin via SSH

Stream:

- VOD restrictions means the stream has to reset every 12 hours
- overlay so I can warn people when stream is going down
- leaderboard website

Ok if I can get through all of that:

Promote:

- sell merch, setup merch store
- setup token store
- sell names on cars
- sell sponsorship banners
- wager on cars
  - token currency
  - currency can be redeemed for merch
  - offsite betting site super dark
