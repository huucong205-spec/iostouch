-- IOSTouch Full Lua
log("Device:", device.name, device.ios)

for step = 1, 3 do
  log("Step", step)
  sleep(0.2)
end

appRun("com.apple.Preferences")
