# Arbos (>1000 line infinitely adaptable agent)

All you need is a Ralph loop and a telegram bot.

## Getting started

```sh
curl -fsSL https://raw.githubusercontent.com/unconst/Arbos/main/run.sh | bash
```

## Next steps

You can just ask how things work
```
# <prompt>
How do you work
```

The main thing is creating agents which run continously on a ralph-loop: calling the same prompt over and over with a delay between calls.
```
# /agent <name> <delay between runs> <prompt>
/agent quant 600 Using my hyperliquid account build out a state of the art quant trading system. 
```

You can send them messages which they get at the beginning of their next loop iteration.
```
# /message <name> <message>
/agent quant Lets rewrite our ML architecture using the latest in timeseries foundation models
```

You can give them environment vars for tools
```
# /env KEY=VALUE <description>
/env MY_HYPERLIQUID_KEY=******* Use this for trading hyperliquid
```

Or do what ever you want by coding features directly into the bot. This updates the code and restarts the agent.
```
# /adapt <prompt>
/adapt I want you to add a new command /pause <agent> which pauses a running agent
```

MIT 

