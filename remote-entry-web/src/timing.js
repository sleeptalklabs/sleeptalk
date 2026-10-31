export function pollSchedule(previous, progress) {
  return { progress, delay: previous?.progress === progress ? Math.min(previous.delay * 2, 10000) : 2000 };
}
export function retryBackoff(transmissions) {
  return transmissions === 1 ? 2000 : transmissions === 2 ? 4000 : null;
}
