/**
 * GET a URL over HTTPS with Node's default trust (its own CAs, plus
 * NODE_EXTRA_CA_CERTS) and print the status, or the error. The Alpine
 * (node:alpine) containers of tests/docker.py run it.
 *
 *     node fetch.js URL
 */
const https = require("https");

/** Report a failure and exit 1. */
function fail(error) {
  console.log(`failed: ${error.message}`);
  process.exit(1);
}

const request = https.get(process.argv[2], { timeout: 10000 }, (response) => {
  console.log(`status ${response.statusCode}`);
  response.resume();
});
request.on("timeout", () => {
  request.destroy(new Error("timed out"));
});
request.on("error", fail);
