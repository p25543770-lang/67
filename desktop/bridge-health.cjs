"use strict";

// A response from a stale process on the same port is not our Python bridge.
function isBridgeHealthValid(statusCode, payload, instance) {
  return statusCode === 200 && payload?.ok === true && payload?.instance === instance;
}

module.exports = { isBridgeHealthValid };
