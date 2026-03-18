import minescript
import time

pos = minescript.player()
minescript.echo(f"BEFORE: {pos}")

minescript.chat("#goto 50 64 50")
time.sleep(20)

pos = minescript.player()
minescript.echo(f"AFTER: {pos}")
