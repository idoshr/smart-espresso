// The dashboard's shell, cached so the page opens instantly on the LAN and
// still opens (slightly stale, clearly marked so by the page itself) when
// the machine's server hiccups. Sensor data is never cached: /api/* always
// goes to the network.
const CACHE = 'espresso-shell-v77';
