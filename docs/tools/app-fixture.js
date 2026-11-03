/**
 * Stands in for app/src/bridge.js when the desktop app's page is rendered for
 * the documentation screenshots: `window.hardcode` answers from fixture data
 * (fixture-data.json, written by fixture-data.py) instead of running the CLI.
 *
 * `?view=<a nav view | new-profile | console | logs>` and `&scope=all` choose
 * what is shown; `&q=` types into the allowlist's search box. The nav views
 * are overview, langs, extensions, allowlist, blocked, forwards and mounts.
 * On the overview and extensions, the VS Code container is still installing
 * extensions, so the status pill and the Extensions view show it.
 */
'use strict';

(() => {
  const params = new URLSearchParams(location.search);
  const view = params.get('view') || 'overview';
  const fixture = fetch('fixture-data.json').then((response) => response.json());

  const outputCallbacks = [];
  const doneCallbacks = [];

  // The newer release the sidebar offers in the update screenshot.
  const UPDATE = {
    version: '0.3.0',
    current: '0.2.0',
    notes: 'https://github.com/you/hardcode/releases/tag/v0.3.0',
  };

  // The profile the page shows; the allowlist, languages, extensions and
  // image come from the fixture.
  const state = {
    home: '/home/you/.config/hardcode',
    profile: 'default',
    profiles: [
      { name: 'default', port: '11111' },
      { name: 'old-node', port: '11112' },
      { name: 'client-x', port: '11113' },
    ],
    env: {},
    vscodePort: '11111',
    allowlist: [],
    forwards: [
      { port: 3000, host: 'vscode', target: 3000 },
      { port: 8081, host: 'docker', target: 80 },
    ],
    mounts: [
      { local: '${HOME}/Git', remote: '/home/dev/git', mode: '' },
      { local: '${HOME}/datasets', remote: '/home/dev/datasets', mode: 'ro' },
    ],
    langs: [],
    extensions: [],
    image: { built: null, languages: null },
    rebuildNeeded: null,
    docker: true,
    busy: null,
  };

  // What the other profiles are built around, for the new-profile dialog's "copy of".
  const otherLanguages = {
    'old-node': ['node', 'build_essentials', 'docker', 'claude_cli', 'codex_cli'],
    'client-x': ['java', 'python', 'docker', 'claude_cli', 'codex_cli'],
  };

  const services = [
    { service: 'allowlist', state: 'exited', status: 'Exited (0) 2 hours ago', health: '' },
    { service: 'pki', state: 'exited', status: 'Exited (0) 2 hours ago', health: '' },
    { service: 'coredns', state: 'running', status: 'Up 2 hours', health: '' },
    { service: 'squid', state: 'running', status: 'Up 2 hours (healthy)', health: 'healthy' },
    { service: 'docker', state: 'running', status: 'Up 2 hours (healthy)', health: 'healthy' },
    { service: 'vscode', state: 'running', status: 'Up 2 hours', health: '' },
    { service: 'gateway', state: 'running', status: 'Up 2 hours', health: '' },
  ];

  // How far the VS Code container has got with the extensions: installing
  // on the Overview and Extensions screenshots, done on the others. Go's and
  // Codex's extensions are still on their way, and one of the profile's own
  // was refused (its publisher has no verified domain).
  const installing = view === 'overview' || view === 'extensions';
  const pending = ['golang.go', 'openai.chatgpt'];
  const refused = ['somedev.handy-snippets'];

  // What `hardcode up` prints, for the console screenshot.
  const startOutput = [
    '[+] Running 7/7',
    ' ✔ Container hardcode-default-allowlist-1  Exited',
    ' ✔ Container hardcode-default-pki-1        Exited',
    ' ✔ Container hardcode-default-coredns-1    Started',
    ' ✔ Container hardcode-default-squid-1      Healthy',
    ' ✔ Container hardcode-default-docker-1       Started',
    ' ✔ Container hardcode-default-vscode-1     Started',
    ' ✔ Container hardcode-default-gateway-1    Started',
  ];

  // What squid's container logged, for the logs dialog's screenshot.
  const squidLog = [
    '2026/10/09 08:02:11| Starting Squid Cache version 6.13 for x86_64-pc-linux-gnu...',
    '2026/10/09 08:02:11| Service Name: squid',
    '2026/10/09 08:02:11| Process ID 1',
    '2026/10/09 08:02:11| With 65536 file descriptors available',
    '2026/10/09 08:02:11| Initializing IP Cache...',
    '2026/10/09 08:02:11| helperOpenServers: Starting 5/5 \'security_file_certgen\' processes',
    '2026/10/09 08:02:12| Accepting SSL bumped HTTP Socket connections at conn3 local=0.0.0.0:3128 remote=[::] FD 14 flags=9',
    '1791533001.231    412 172.31.253.10 TCP_TUNNEL/200 6721 CONNECT api.anthropic.com:443 - HIER_DIRECT/160.79.104.10 -',
    '1791533002.784     88 172.31.253.10 TCP_MISS/200 2214 GET https://pypi.org/simple/requests/ - HIER_DIRECT/151.101.0.223 application/vnd.pypi.simple.v1+json',
    '1791533003.102      0 172.31.253.10 TCP_DENIED/403 3942 GET https://api.github.com/repos/someone-else/tool - HIER_NONE/- text/html',
    '1791533004.950    131 172.31.253.10 TCP_MISS/200 51203 GET https://registry.npmjs.org/typescript - HIER_DIRECT/104.16.24.34 application/json',
    '1791533007.418      0 172.31.253.12 TCP_DENIED/403 3942 PUT https://github.com/ourcompany/app.git/git-receive-pack - HIER_NONE/- text/html',
    '1791533009.006    203 172.31.253.10 TCP_MISS/200 8812 GET https://github.com/ourcompany/app.git/info/refs - HIER_DIRECT/140.82.112.4 application/x-git-upload-pack-advertisement',
  ].join('\n');

  /** The profile's state, with the parts that come from the fixture data. */
  async function stateWithFixture() {
    const data = await fixture;
    state.allowlist = data.allowlist;
    state.langs = data.langs;
    state.extensions = data.extensions;
    state.image = data.image;
    state.rebuildNeeded = data.rebuildNeeded;
    state.rebuildReasons = data.rebuildReasons;
    state.packages = data.packages;
    state.lan = data.lan;
    return state;
  }

  /**
   * The VS Code container's extension status, as extensions:status answers:
   * the refused one isn't counted in the total, the pending ones aren't
   * installed yet.
   */
  async function extensionStatus() {
    const data = await fixture;
    const wanted = new Set(data.extensions.filter((row) => row.enabled).map((row) => row.id));
    const total = wanted.size - refused.length;
    const waiting = installing ? pending : [];

    return {
      state: installing ? 'installing' : 'done',
      total,
      installed: total - waiting.length,
      pending: waiting,
      refused,
      failed: [],
      updated: '2026-10-09T08:04:12Z',
    };
  }

  /** What stack:status answers: the containers, and how far the extensions have got. */
  async function stackStatus() {
    const status = await extensionStatus();
    const extensions = { state: status.state, installed: status.installed, total: status.total };

    return { services, docker: true, error: '', extensions };
  }

  /** Another profile's languages: the catalogue, switched on as that profile has it. */
  async function languagesOf(name) {
    const data = await fixture;
    const chosen = otherLanguages[name] || [];
    return data.langs.map((lang) => ({ ...lang, enabled: chosen.includes(lang.key) }));
  }

  /** Print the start output line by line, and never finish (the console stays busy). */
  function streamStart() {
    return new Promise(() => {
      startOutput.forEach((line, index) => {
        setTimeout(() => {
          outputCallbacks.forEach((callback) => callback({ name: 'start', id: 'start#1', line }));
        }, 100 * index);
      });
    });
  }

  const handlers = {
    'home:get': () => ({ home: state.home, version: '0.1.0', profile: state.profile, catalogue: null }),
    'home:open': () => null,
    'vscode:port': stateWithFixture,
    'state:get': stateWithFixture,
    'stack:status': stackStatus,
    'vscode:url': () => 'http://localhost:11111/?tkn=k3Zb7Qp1xJ9vL2mN8rT4wY6aC0eF5gH',
    'vscode:rotate': () => [],
    'traffic:blocked': async () => (await fixture).blocked,
    'image:get': async () => (await fixture).imageSteps,
    // The names on your network, each still resolving where it was approved.
    'lan:list': async () => (await fixture).lan.map((pin) => ({ ...pin, resolves: [pin.address], state: 'ok' })),
    'stack:logs': () => squidLog,
    'extensions:status': extensionStatus,
    'profiles:languages': languagesOf,
    'smoke:mode': () => false,
    'stack:start': streamStart,
    // A newer release to offer (?update=1), for its screenshot; else none.
    'update:check': () => (params.get('update') ? UPDATE : null),
  };

  window.hardcode = {
    // Any other channel answers with the profile's state, as most do.
    invoke: (channel, ...args) => Promise.resolve((handlers[channel] || stateWithFixture)(...args)),
    onOutput: (callback) => outputCallbacks.push(callback),
    onDone: (callback) => doneCallbacks.push(callback),
    platform: 'Linux',
  };

  // -- drive the page to the view being documented

  /** The first element on the page that matches *selector*. */
  const $ = (selector) => document.querySelector(selector);

  /**
   * Show what went wrong in the fixture over the page, so that the
   * screenshot can't pass for the view it was meant to show.
   */
  const fail = (error) => {
    const banner = document.createElement('pre');
    banner.textContent = `the screenshot fixture failed: ${error}`;
    banner.style.cssText = 'position:fixed;inset:40% 10% auto;padding:20px;background:#b00020;color:#fff;z-index:9999';
    document.body.appendChild(banner);
  };

  /** A promise that resolves once *condition* holds; a condition that throws is shown, and never holds. */
  const waitFor = (condition) =>
    new Promise((resolve) => {
      const timer = setInterval(() => {
        let holds;
        try {
          holds = condition();
        } catch (error) {
          clearInterval(timer);
          fail(error);
          return;
        }

        if (holds) {
          clearInterval(timer);
          resolve();
        }
      }, 50);
    });

  window.addEventListener('load', async () => {
    await waitFor(() => $('#home-path').textContent && $('#status-pill').textContent.startsWith('running'));

    if (view === 'new-profile') {
      $('#btn-new-profile').click();
      $('#np-name').value = 'client-y';
      return;
    }

    if (view === 'console') {
      $('#btn-start').click();
      return;
    }

    if (view === 'logs') {
      await waitFor(() => $('[data-logs="squid"]'));
      $('[data-logs="squid"]').click();
      return;
    }

    $(`.nav-item[data-view="${view}"]`).click();

    if (params.get('scope') === 'all') {
      await waitFor(() => $('[data-scope="all"]'));
      $('[data-scope="all"]').click();
    }

    if (view === 'blocked') {
      await waitFor(() => $('#view tbody tr td.mono'));
    }

    if (view === 'allowlist' && params.get('q')) {
      const search = $('#al-search');
      search.value = params.get('q');
      search.dispatchEvent(new Event('input'));
    }
  });
})();
