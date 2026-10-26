# Work log

## Tool permissions
Shell is advertised and executed only with explicit shell permission. Browser calls are also refused when disabled, even if the model invents a call. Graph specs now accept shell as an explicit tool. Offline tests cover denied calls and an enabled local shell command. No new environment variables.

## Shell timeouts
Each shell command starts its own process group. A timeout kills the whole group and drains/reaps the shell before returning an error, so child commands do not keep running. A local sleep regression verifies the shell and its child stop. No new environment variables.
