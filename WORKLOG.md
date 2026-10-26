# Work log

## Tool permissions
Shell is advertised and executed only with explicit shell permission. Browser calls are also refused when disabled, even if the model invents a call. Graph specs now accept shell as an explicit tool. Offline tests cover denied calls and an enabled local shell command. No new environment variables.

## Shell timeouts
Each shell command starts its own process group. A timeout kills the whole group and drains/reaps the shell before returning an error, so child commands do not keep running. A local sleep regression verifies the shell and its child stop. No new environment variables.

## Observer identity and paths
The observer derives senders from the NATS subject and the graph's channel sources, including after restart. User input subjects remain user messages. Markdown writes require a slug sender; message-body src cannot choose a path or impersonate another sender in the traffic log. Offline subscription tests cover spoofing, channels, input, and invalid subjects. No new environment variables.

## Conversation memory
History is now per conversation, with a 50-conversation LRU and the existing 20-message limit per conversation. Prompts start a conv from their message id; automatic replies and send-tool messages retain it and link prev to the triggering message. Missing conv defaults to the incoming message id. Offline tests cover isolation, continuation, eviction, length limits, and both send routes. No new environment variables.
