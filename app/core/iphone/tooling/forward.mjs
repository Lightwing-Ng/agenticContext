import net from 'node:net';
import iosDevice from 'appium-ios-device';

const [udid, localArgument = '18100', deviceArgument = '8100'] = process.argv.slice(2);
if (!/^[A-Za-z0-9-]{8,64}$/.test(udid ?? '')) throw new Error('Provide a valid device UDID.');
function port(value) {
  if (!/^\d+$/.test(value)) throw new Error('Ports must be integers.');
  const number = Number(value);
  if (!Number.isInteger(number) || number < 1024 || number > 65535) throw new Error('Ports must be 1024-65535.');
  return number;
}
const localPort = port(localArgument);
const devicePort = port(deviceArgument);
const sockets = new Set();
let stopping = false;
const server = net.createServer(async client => {
  client.setNoDelay(true);
  sockets.add(client);
  client.on('error', () => client.destroy());
  client.on('close', () => sockets.delete(client));
  try {
    const remote = await iosDevice.utilities.connectPort(udid, devicePort);
    if (stopping || client.destroyed) { remote.destroy(); return; }
    remote.setNoDelay?.(true);
    sockets.add(remote);
    remote.on('error', error => { console.error(`device socket: ${error.message}`); client.destroy(); });
    remote.on('close', () => { sockets.delete(remote); client.destroy(); });
    client.on('close', () => remote.destroy());
    client.pipe(remote).pipe(client);
  } catch (error) {
    console.error(`USB connect failed: ${error.message}`);
    client.destroy();
  }
});
server.on('error', error => { console.error(`USB forward failed: ${error.message}`); process.exitCode = 1; stop(); });
server.listen(localPort, '127.0.0.1', () => console.log(`WDA USB forward listening at 127.0.0.1:${localPort}`));
function stop() {
  if (stopping) return;
  stopping = true;
  for (const socket of sockets) socket.destroy();
  server.close(() => process.exit(process.exitCode ?? 0));
  setTimeout(() => process.exit(process.exitCode ?? 0), 1000).unref();
}
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
