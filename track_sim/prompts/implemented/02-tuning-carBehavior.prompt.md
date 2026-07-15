# tuning the car behavior

cars want to hit their top speeds above all else
cars want to go faster than their previous times
cars do not want to take damage
cars do not want to slow down
cars want to keep their nose going forward
cars are ok with losing control as long as it doesn't slow them down
cars do not want to hit barriers

cars remember their previous 10 races and learn from them
cars will also track their laptime and use it to adjust their driving strategy
each car remembers its own performance and adapts its strategy accordingly to maximize its chances of winning

this creates a simple reinforcement learning scenario for the cars, where they continuously improve their performance based on past experiences.

this needs to be expressed as a data type, such as a class or struct, that captures the car's behavior, memory, and learning capabilities.

## track simulator allows loading of multiple cars

load map, load car, move the car around to place it, load another car, and repeat as needed. 

saving the map will preserve the current layout of cars and the track configuration.

tracks will need to remember the starting positions of all cars.

resetting the map will put the cars back into their original starting positions.

cars can't be duplicated, each car must be unique. you can load the same car but it will be number suffixed to make it unique.

the stats window will need a dropdown to select which car's stats to display.