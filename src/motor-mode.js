// UI gate only. The Python bridge repeats these checks and owns the motor
// cooldown/scan interlock; a disabled button is not a hardware safety device.
export function tiltAvailability({ visible, environmentReady, sdkSource, connected,
                                   scanInProgress, deviceBusy, scanBusy, motorBusy,
                                   queryBusy, angleKnown, cooldownMs }) {
  const base = Boolean(visible && environmentReady && sdkSource && connected &&
    !scanInProgress && !deviceBusy && !scanBusy && !motorBusy);
  return {
    read: base && !queryBusy,
    select: base && angleKnown,
    apply: base && angleKnown && !queryBusy && Number.isFinite(cooldownMs) && cooldownMs <= 0,
  };
}
