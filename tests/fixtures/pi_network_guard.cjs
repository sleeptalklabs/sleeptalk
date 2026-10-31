// Only the local fixture provider may be contacted by a real Pi release.
const fs = require('node:fs');
const net = require('node:net');
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
  const options = Array.isArray(args[0]) ? args[0][0] : args[0];
  if (options && typeof options === 'object' && options.host === '127.0.0.1'
      && String(options.port) === process.env.SLEEPTALK_TEST_PORT) {
    return connect.apply(this, args);
  }
  fs.writeFileSync(process.env.SLEEPTALK_TEST_NETWORK_MARKER, 'unexpected network attempt');
  throw new Error('Only the local Pi fixture provider is allowed');
};
require('node:module').syncBuiltinESMExports();
