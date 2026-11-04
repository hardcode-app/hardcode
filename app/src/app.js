/**
 * The HardCode desktop app's page.
 *
 * Everything the app does goes through `window.hardcode` (bridge.js), which
 * runs the `hardcode` command line; this file only shows the results and
 * turns clicks into calls. Each view is a function that returns its HTML;
 * `render()` puts the current one on the page and `bind()` wires up its
 * controls.
 *
 * A profile is built around its languages and tools: each one opens
 * allowlist entries, brings VS Code extensions and puts its toolchain or CLI
 * in the image. So the Languages / Tools view comes right after the
 * Overview, a new profile starts with choosing them, and the Extensions and
 * Allowlist views say what came from which language. The image has the
 * profile's own Ubuntu packages too (the Languages / Tools view's last card)
 * and its own Dockerfile steps (the Image view), built on HardCode's.
 *
 * The extensions aren't in the image: a running VS Code container installs
 * and removes them in the background, within seconds of a change. The page
 * follows that from the status the container writes (`extensions` in
 * stack:status, and the whole of it from extensions:status).
 */
'use strict';

const api = window.hardcode;

// What the page is showing, and what it knows.
const state = {
  view: 'overview',
  data: null, // the profile's state, from state:get
  status: { services: [], docker: null, error: '', extensions: null, proxy: null },
  statusTimer: null, // the next status refresh
  epoch: 0, // moves on when another profile is shown; replies asked for the last one are dropped
  shownProfile: null, // the profile the page shows
  blocked: [],
  blockedSince: '30m',
  allowlistFilter: 'all',
  allowlistQuery: '',
  scope: 'profile', // 'profile' or 'all': what allowlist, language, extension and mount edits apply to
  // How far the running VS Code container has got with the profile's extensions,
  // from extensions:status: {state, total, installed, pending, refused, failed}.
  // Null while it isn't running, or hasn't said yet.
  extensionStatus: null,
  extensionStatusAsked: false, // whether extensions:status has answered for the profile shown
  extensionStatusError: '', // why the VS Code container couldn't be asked, when it couldn't
  extensionWatchUntil: 0, // until when (Date.now()) the status is read often, after a change to the extensions
  proxyWatchUntil: 0, // ... and after a change the proxy is given in the background
  extensionDraft: '', // what is typed in the Extensions view's form
  packageDraft: '', // what is typed in the Ubuntu packages card's form
  imageSteps: null, // the profile's own Dockerfile steps, from image:get; null until it answers
  imageStepsError: '', // why image:get couldn't answer, when it couldn't
  imageDraft: null, // the Image view's editor, once edited and not yet saved; null: what the file says
  pickRequest: 0, // the new-profile dialog's last language lookup; answers to older ones are dropped
  firstProfile: false, // the new-profile dialog is making the home's first profile, and can't be dismissed
  catalogue: null, // every language and tool, before the first profile (state.data has them after)
  update: null, // a newer release, from update:check: {version, current, notes}
  updateCheck: null, // the start-up check, a promise; the smoke run waits for it
  running: null, // the name of the command whose output is streaming to the console
  runningId: null, // its stream id, for the Stop button
  prefill: null, // {entry, flag} handed from Blocked traffic to the allowlist form
  lanStates: null, // the names on your network as lan:list last looked them up, by name; null until it answers
  adding: null, // {entry, flag}: the allowlist entry being added, shown until the CLI answers
  vscodeUrl: '', // VS Code's URL with its token, once the running VS Code container has told it
  vscodeError: '', // why it couldn't, when it couldn't
  vscodeFetching: false,
  showToken: false, // whether the URL box shows the token, or hides it
  logsService: null, // the container whose logs the logs dialog shows
  logsTimer: null,
  deviceDialog: null, // the key of the device the dialog would turn on, while it is open
};

const TITLES = {
  overview: 'Overview',
  langs: 'Languages / Tools',
  image: 'Image',
  extensions: 'Extensions',
  allowlist: 'Allowlist',
  blocked: 'Blocked traffic',
  forwards: 'Port forwards',
  mounts: 'Mounts',
};

// The allowlist's filter chips, and what each one says.
const ALLOWLIST_FILTERS = {
  all: 'all',
  enabled: 'enabled',
  disabled: 'disabled',
  path: 'path',
  rw: 'rw',
  ssh: 'ssh',
  provided: 'provided',
  custom: 'your own',
};

// How a change's toast says that the running sandbox took it, or that it
// waits for the next start. A language's words depend on what it brings
// (languageOutcome).
const ALLOWLIST_OUTCOME = { applied: 'the proxy reloads in a few seconds', waiting: 'applies when the sandbox starts' };
const EXTENSION_ADDED = { applied: 'installing in the background', waiting: 'applies when the sandbox starts' };
const EXTENSION_REMOVED = { applied: 'removing in the background', waiting: 'applies when the sandbox starts' };
const DOCKER_OUTCOME = { applied: 'containers recreated', waiting: 'applies when the sandbox starts' };
const DEVICE_OUTCOME = { applied: 'VS Code container recreated', waiting: 'applies when the sandbox starts' };
// The profile's own packages change nothing that runs: the image has them after its next rebuild.
const PACKAGE_OUTCOME = { applied: '', waiting: '' };

// Debian's rule for a package's name, which the CLI checks too (cli/hardcode_cli/packages.py):
// lower-case letters, digits, +, - and ., at least two characters, starting with a letter or digit.
const PACKAGE_NAME = /^[a-z0-9][a-z0-9+.-]+$/;

// One-shot containers that exit when their work is done; not worth a status row.
const SETUP_SERVICES = ['allowlist', 'pki'];

// The CLI's refusal of the Docker snap, and the address of HardCode's own
// docs it ends with: only those become a link.
const DOCKER_SNAP = /^Docker on this computer is the snap\b/;
const DOCS_LINK = /https:\/\/github\.com\/hardcode-app\/hardcode\/blob\/main\/docs\/[\w./#-]+$/;

// How often the header's status is read (milliseconds): usually, and while
// the VS Code container installs extensions, so their progress shows.
const STATUS_REFRESH = 15000;
const STATUS_REFRESH_INSTALLING = 3000;

// How long after a change to the extensions the status is read that often
// anyway: the container notices the change within a few seconds, and only
// then says it is installing.
const EXTENSION_WATCH = 15000;

// ... and after a change to the allowlist: the proxy is given it five seconds
// after the last change, and the reload takes some seconds more.
const PROXY_WATCH = 30000;

// How much of a container's log the logs dialog shows, and how often it is read again.
const LOG_LINES = 300;
const LOG_REFRESH = 3000; // milliseconds

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** The first element on the page that matches *selector*. */
const $ = (selector) => document.querySelector(selector);

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

/** Text made safe to put into HTML, attributes included. */
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ESCAPES[character]);

/** How many profiles there are; one until they are known. */
const profileCount = () => (state.data ? state.data.profiles.length : 1);

/** A promise that resolves after *milliseconds*. */
const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

/** A count and the word that goes with it: "1 entry", "2 entries". */
function counted(count, one, many) {
  return `${count} ${count === 1 ? one : many}`;
}

/** Names as a sentence lists them: "A", "A and B", "A, B and C". */
function joinNames(names) {
  if (names.length < 2) {
    return names.join('');
  }

  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

/** The catalogue: the shown profile's, each marked on or off; before the first profile, the bare one. */
function catalogue() {
  return state.data ? state.data.langs : state.catalogue || [];
}

/** The catalogue entry of the language or tool *key*. */
function langOf(key) {
  return catalogue().find((lang) => lang.key === key);
}

/** The name of the language or tool *key*, as the page shows it. */
function langName(key) {
  const lang = langOf(key);
  return lang ? lang.name : key;
}

/** Whether *key* is a language or a tool, in words: "language" or "tool". */
function langKind(key) {
  const lang = langOf(key);
  return lang ? lang.kind : 'language';
}

/** The keys of the languages and tools that are on, in a list of catalogue entries. */
function enabledKeys(languages) {
  return languages.filter((lang) => lang.enabled).map((lang) => lang.key);
}

/** A message in the corner that goes away by itself; errors stay longer. */
function toast(message, kind = '') {
  const element = document.createElement('div');
  element.className = `toast ${kind}`;
  element.textContent = message;
  $('#toasts').appendChild(element);

  setTimeout(() => element.remove(), kind === 'error' ? 7000 : 3500);
}

/** What went wrong, as a line the user can read. */
function errorText(error) {
  return String(error.message || error);
}

/**
 * Tell the user what the last change did, from the CLI's own results:
 * whether the running sandbox took it, or it waits for the next start.
 * Nothing already in force is shown as "pending". A change the running
 * proxy is given in the background is followed until it has it.
 *
 * *outcome* has the words for both (the allowlist's by default). *head*,
 * when given, says what changed in place of the CLI's message.
 */
function reportOutcome(outcome = ALLOWLIST_OUTCOME, head = '') {
  const results = (state.data && state.data.lastResults) || [];
  if (!results.length) {
    return;
  }

  if (results.some((result) => result.reloading)) {
    watchProxy();
  }

  const applied = results.filter((result) => result.applied === true).length;
  const waiting = results.filter((result) => result.applied === false).length;
  const skipped = results.filter((result) => /^skipped/.test(result.message)).length;
  const changed = results.filter((result) => result.changed).length;

  let summary = `${changed} of ${results.length} profiles changed`;
  if (results.length === 1) {
    summary = head && results[0].changed ? head : results[0].message;
  }

  const now = applied && outcome.applied ? ` · ${outcome.applied}` : '';
  const later = waiting ? ` · ${outcome.waiting}` : '';

  toast(`${summary}${now}${later}`, skipped ? '' : 'ok');
}

/**
 * The words for switching *lang* on or off: what a running sandbox does by
 * itself (the proxy reloads with its allowlist block in a few seconds; its
 * VS Code container installs or removes its extensions in the background),
 * or that it waits for the next start. The toolchain waits for a rebuild,
 * which the Languages / Tools view says on its own.
 */
function languageOutcome(lang, enabled) {
  const now = [];
  if (lang.domains.length) {
    now.push('the proxy reloads in a few seconds');
  }
  if (lang.extensions.length) {
    now.push(enabled ? 'extensions installing' : 'extensions removing');
  }

  return { applied: now.join(' · '), waiting: 'applies when the sandbox starts' };
}

/**
 * What the toast says as an allowlist entry is being added: that, and where
 * to. A running proxy is given it in the background afterwards.
 */
function addingMessage(entry) {
  return state.scope === 'all' ? `Adding ${entry} to every profile…` : `Adding ${entry}…`;
}

/** Whether the running sandbox took any of the last change's results. */
function changeApplied() {
  const results = (state.data && state.data.lastResults) || [];
  return results.some((result) => result.applied === true);
}

/**
 * Call the bridge, showing a failure as a toast. The error is still thrown,
 * so the caller can stop; it is marked as already reported.
 */
async function call(channel, ...args) {
  try {
    return await api.invoke(channel, ...args);
  } catch (error) {
    toast(errorText(error), 'error');
    if (error && typeof error === 'object') {
      error.toasted = true;
    }
    throw error;
  }
}

/**
 * Call the bridge for a change to the profile. When it fails, the profile is
 * read again and shown as it is: a switch the click flipped goes back, and a
 * change the CLI wrote before applying it failed shows up.
 */
async function change(channel, ...args) {
  try {
    return await call(channel, ...args);
  } catch (error) {
    await loadState().catch(() => {});
    throw error;
  }
}

// A rejection that was already shown as a toast isn't an unhandled error.
window.addEventListener('unhandledrejection', (event) => {
  if (event.reason && event.reason.toasted) {
    event.preventDefault();
  }
});

// ---------------------------------------------------------------------------
// The console drawer, where long commands stream their output
// ---------------------------------------------------------------------------

const consoleOutput = $('#console-out');

/** Open or close the console drawer. */
function openConsole(open) {
  $('#console').classList.toggle('collapsed', !open);
  $('#console-toggle').textContent = (open ? '▾' : '▴') + ' Console';
}

/** Say in the console's head which command is running. */
function showRunning(name) {
  $('#console-status').innerHTML = `<span class="spin"></span>${esc(name)}…`;
}

$('#console-toggle').onclick = () => openConsole($('#console').classList.contains('collapsed'));
$('#console-clear').onclick = () => {
  consoleOutput.textContent = '';
};

api.onOutput(({ name, id, line }) => {
  // The first line of a new command opens the drawer and names it.
  if (state.running !== name || state.runningId !== id) {
    state.running = name;
    state.runningId = id;
    openConsole(true);
    showRunning(name);
    setButtons();
  }

  consoleOutput.textContent += line + '\n';
  consoleOutput.scrollTop = consoleOutput.scrollHeight;
});

api.onDone(({ name, code }) => {
  state.running = null;
  state.runningId = null;

  let outcome = `failed (exit ${code})`;
  if (code === 0) {
    outcome = 'done';
  }
  if (code === -1) {
    outcome = 'stopped';
  }
  $('#console-status').textContent = `${name}: ${outcome}`;

  setButtons();
  refreshStatus();
});

$('#console-stop').onclick = () => {
  if (state.runningId) {
    api.invoke('stream:cancel', state.runningId).catch((error) => toast(errorText(error), 'error'));
  }
};

/**
 * Run a long command. The page is busy from the click, not from the first
 * line of output, so a second click can't start it twice.
 */
async function start(name, channel, ...args) {
  state.running = name;
  setButtons();
  openConsole(true);
  showRunning(name);

  try {
    return await call(channel, ...args);
  } finally {
    // A command that never streamed (it failed straight away) frees the page here.
    if (!state.runningId) {
      state.running = null;
      setButtons();
    }
  }
}

// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------

/** Read the profile's state, fill in the profile switcher, and show the view again. */
async function loadState() {
  state.data = await call('state:get');
  if (state.data.profile !== state.shownProfile) {
    forgetProfile();
    state.shownProfile = state.data.profile;
  }

  showHome(state.data.home);

  const options = state.data.profiles.map((profile) => {
    const selected = profile.name === state.data.profile ? 'selected' : '';
    return `<option value="${esc(profile.name)}" ${selected}>${esc(profile.name)} · :${esc(profile.port)}</option>`;
  });
  $('#profile-select').innerHTML = options.join('');

  if (profileCount() < 2) {
    state.scope = 'profile';
  }
  render();
}

/**
 * Read the profile again after a command that may have built its image
 * (Start, Rebuild images), so what the image has is shown as it now is.
 * Only the views that show it are drawn again: a form being filled in
 * elsewhere is left alone.
 */
async function reloadAfterBuild() {
  const epoch = state.epoch;
  const data = await call('state:get');

  // Asked for the profile shown before a switch.
  if (epoch !== state.epoch) {
    return;
  }

  state.data = data;
  if (state.view === 'overview' || state.view === 'langs') {
    render();
  }
}

/**
 * Ask Compose what is running, and show it in the header's status pill;
 * then read it again later, sooner while extensions are installing. Every
 * read, on the timer or after a command, schedules the next.
 */
async function refreshStatus() {
  try {
    await readStatus();
  } finally {
    scheduleStatus();
  }
}

/** Read the status once, and show it; there is none before the first profile. */
async function readStatus() {
  if (!state.data) {
    return;
  }

  const epoch = state.epoch;
  const wasWatching = watchingExtensions();
  const failedBefore = proxyState().error;

  let status;
  try {
    status = await call('stack:status');
  } catch (error) {
    status = { services: [], docker: null, error: errorText(error), extensions: null, proxy: null };
  }

  // Asked for the profile shown before a switch: the new one's comes next.
  if (epoch !== state.epoch) {
    return;
  }
  state.status = status;
  showStatusPill();

  // A background reload of the proxy that failed is said once, as it fails;
  // the status pill keeps saying so until the proxy has the allowlist.
  const failed = proxyState().error;
  if (failed && failed !== failedBefore) {
    toast(`The proxy didn't take the allowlist: ${failed}`, 'error');
  }

  // A VS Code container that stopped takes its token's validity with it, as far as we
  // know; a running one is asked for its URL until it gives it.
  if (!isUp()) {
    forgetVscodeUrl();
  } else if (!state.vscodeUrl) {
    fetchVscodeUrl();
  }

  setButtons();
  if (state.view === 'overview') {
    render();
  }

  // The Extensions view follows the installing extension by extension, and
  // is read once more when it ends, for how it ended.
  if (state.view === 'extensions' && (wasWatching || watchingExtensions())) {
    fetchExtensionStatus();
  }
}

/**
 * Read the status again in a while; in a few seconds while extensions are
 * being installed or removed, or the proxy is being given the allowlist.
 */
function scheduleStatus() {
  clearTimeout(state.statusTimer);

  const delay = watchingExtensions() || watchingProxy() ? STATUS_REFRESH_INSTALLING : STATUS_REFRESH;
  state.statusTimer = setTimeout(refreshStatus, delay);
}

/** How the proxy stands with the allowlist, as the last status said: {reloading, behind, error}. */
function proxyState() {
  return state.status.proxy || { reloading: false, behind: false, error: '' };
}

/**
 * Whether the proxy is being given the allowlist: its background reload
 * runs, or a change was made a moment ago and the reload is about to start.
 */
function proxyApplying() {
  const proxy = proxyState();
  const justChanged = Date.now() < state.proxyWatchUntil;
  return proxy.reloading || (proxy.behind && justChanged && !proxy.error);
}

/** Whether the proxy is worth following closely: it is being given the allowlist, or was a moment ago. */
function watchingProxy() {
  return proxyApplying() || Date.now() < state.proxyWatchUntil;
}

/**
 * After a change the running proxy is given in the background: follow it
 * from now on, until it has it.
 */
function watchProxy() {
  state.proxyWatchUntil = Date.now() + PROXY_WATCH;
  refreshStatus();
}

/** Whether the running VS Code container is installing extensions. */
function extensionsInstalling() {
  const extensions = state.status.extensions;
  return isUp() && !!extensions && extensions.state === 'installing';
}

/**
 * Whether the extensions are worth following closely: the running VS Code
 * container is installing them, or they changed a moment ago and it is
 * about to.
 */
function watchingExtensions() {
  const justChanged = Date.now() < state.extensionWatchUntil;
  return extensionsInstalling() || (isUp() && justChanged);
}

/**
 * After a change to the extensions that the running sandbox took: follow
 * the VS Code container installing or removing them, from now on.
 */
function watchExtensions() {
  state.extensionWatchUntil = Date.now() + EXTENSION_WATCH;
  refreshStatus();
}

/**
 * The header's status pill: what runs and, while the VS Code container
 * installs extensions, how far it has got ("running (5) · extensions 3/12");
 * while the proxy is being given the allowlist, that ("· applying
 * allowlist"), and when the running one hasn't the allowlist, that too.
 */
function showStatusPill() {
  const services = state.status.services;
  const running = services.filter((service) => service.state === 'running').length;
  const total = services.filter((service) => !SETUP_SERVICES.includes(service.service)).length;

  let text = `${running}/${total} running`;
  let kind = 'warn';
  if (state.status.docker === false) {
    text = 'docker not found';
    kind = 'bad';
  } else if (state.status.error) {
    text = 'status unknown';
    if (/Cannot connect to the Docker daemon/i.test(state.status.error)) {
      text = 'docker not running';
    } else if (DOCKER_SNAP.test(state.status.error)) {
      text = 'docker snap unsupported';
    }
    kind = 'bad';
  } else if (!services.length) {
    text = 'not created';
    kind = '';
  } else if (running === total) {
    text = `running (${running})`;
    kind = 'ok';
  } else if (running === 0) {
    text = 'stopped';
  }

  let title = state.status.error || '';
  if (extensionsInstalling()) {
    const extensions = state.status.extensions;
    text += ` · extensions ${extensions.installed}/${extensions.total}`;
    title = `Installing VS Code extensions in the background: ${extensions.installed} of ${extensions.total} in place`;

    // Nothing is wrong: the sandbox runs, and VS Code picks them up as they arrive.
    if (kind === 'ok') {
      kind = 'info';
    }
  }

  const proxy = proxyState();
  if (proxyApplying()) {
    text += ' · applying allowlist';
    title = 'Giving the proxy the allowlist: it reloads with it a few seconds after your last change';
    if (kind === 'ok') {
      kind = 'info';
    }
  } else if (proxy.behind) {
    text += ' · allowlist not applied';
    title = proxy.error
      ? `The proxy didn't take the allowlist: ${proxy.error}. Reload proxy, on the Allowlist page, tries again.`
      : "The running proxy hasn't the allowlist as it is now (edited by hand?): Reload proxy, on the Allowlist page, gives it.";
    kind = proxy.error ? 'bad' : 'warn';
  }

  const pill = $('#status-pill');
  pill.textContent = text;
  pill.className = `pill ${kind}`;
  pill.title = title;
}

/**
 * The header's mark of a weakened sandbox: while the profile shown has
 * devices of this computer on, "weakened: kvm, gpu", with what that means
 * in its tooltip; nothing while it has none, or there is no profile.
 */
function showWeakened() {
  const badge = $('#weakened');
  const on = state.data ? state.data.devices.filter((device) => device.enabled) : [];

  badge.hidden = !on.length;
  badge.textContent = `weakened: ${on.map((device) => device.key).join(', ')}`;
  badge.title = on.length
    ? `This profile's VS Code container can use ${joinNames(on.map((device) => device.name))} of this computer, ` +
      "and each one weakens the sandbox. The Overview's Devices card switches them off."
    : '';
}

/** Whether the profile's VS Code container is running. */
function isUp() {
  return state.status.services.some((service) => service.service === 'vscode' && service.state === 'running');
}

/** Enable the header's buttons for what can be done now. */
function setButtons() {
  const busy = !!state.running;
  const anythingRunning = state.status.services.some((service) => service.state === 'running');

  $('#btn-start').disabled = busy;
  $('#btn-stop').disabled = busy || !anythingRunning;
  $('#btn-vscode').disabled = busy || !state.vscodeUrl;
  $('#btn-vscode').title = state.vscodeUrl ? '' : urlPlaceholder();
  $('#profile-select').disabled = busy;
  $('#console-stop').hidden = !state.runningId;
}

/**
 * Ask the running VS Code container how far it has got with each of the
 * profile's extensions: which are still installing, which it refused (their
 * publisher has no verified domain), which failed. It means a call into the
 * container, so it is asked when the Extensions view opens, and again with
 * each status refresh while extensions are installing. Not being able to
 * ask is said on the view, not in a toast.
 */
async function fetchExtensionStatus() {
  const epoch = state.epoch;

  let status = null;
  let problem = '';
  try {
    status = await api.invoke('extensions:status');
  } catch (error) {
    problem = errorText(error);
  }

  // Asked for the profile shown before a switch.
  if (epoch !== state.epoch) {
    return;
  }

  state.extensionStatus = status;
  state.extensionStatusError = problem;
  state.extensionStatusAsked = true;

  // The header's pill reads the same status: it is kept in step, and so is
  // how often it is read, in case this saw the installing first.
  if (!problem) {
    state.status.extensions = null;
    if (status) {
      state.status.extensions = { state: status.state, installed: status.installed, total: status.total };
    }
    showStatusPill();
    scheduleStatus();
  }

  if (state.view === 'extensions') {
    renderKeepingFocus();
  }
}

/**
 * Read the profile's own Dockerfile steps (where they are, their text, the
 * starter, HardCode's Dockerfile) for the Image view, which asks when it
 * opens and after a save. Not being able to read them is said on the view.
 */
async function fetchImageSteps() {
  const epoch = state.epoch;

  let steps = null;
  let problem = '';
  try {
    steps = await api.invoke('image:get');
  } catch (error) {
    problem = errorText(error);
  }

  // Asked for the profile shown before a switch.
  if (epoch !== state.epoch) {
    return;
  }

  state.imageSteps = steps;
  state.imageStepsError = problem;
  if (state.view === 'image') {
    renderKeepingFocus();
  }
}

// ---------------------------------------------------------------------------
// VS Code's URL
// ---------------------------------------------------------------------------

/** Forget VS Code's URL, and why there was none. */
function forgetVscodeUrl() {
  state.vscodeUrl = '';
  state.vscodeError = '';
}

/**
 * Forget what belonged to the profile shown until now: its URL, status,
 * blocked traffic, how its extensions are doing, and what its forms and
 * the Image view's editor held. Replies still on their way for it are
 * dropped.
 */
function forgetProfile() {
  state.epoch += 1;
  forgetVscodeUrl();
  state.status = { services: [], docker: state.status.docker, error: '', extensions: null, proxy: null };
  state.blocked = [];
  state.lanStates = null;
  state.extensionStatus = null;
  state.extensionStatusAsked = false;
  state.extensionStatusError = '';
  state.extensionWatchUntil = 0;
  state.proxyWatchUntil = 0;
  state.packageDraft = '';
  state.imageSteps = null;
  state.imageStepsError = '';
  state.imageDraft = null;
}

/** What the URL box says while it has no URL to show. */
function urlPlaceholder() {
  if (!isUp()) {
    return 'start the sandbox first';
  }
  if (state.vscodeError) {
    return state.vscodeError;
  }
  return 'fetching token…';
}

/**
 * The URL with its token hidden: the token is a credential, and a screen is
 * seen by more than its owner (a screenshot, a shared screen).
 */
function maskedUrl(url) {
  return url.replace(/([?&]tkn=)[^&#]+/, '$1••••••••');
}

/** Put the URL (or why there is none) on the overview, and enable what needs it. */
function showVscodeUrl() {
  const box = $('#vscode-url');
  if (!box) {
    return;
  }

  box.value = state.showToken ? state.vscodeUrl : maskedUrl(state.vscodeUrl);
  box.placeholder = urlPlaceholder();

  const ready = !!state.vscodeUrl;
  $('#copy-url').disabled = !ready;
  $('#show-token').disabled = !ready;
  $('#show-token').textContent = state.showToken ? 'Hide' : 'Show';
  $('#new-token').disabled = !ready || Boolean(state.running);
  $('[data-act="vscode"]').disabled = !ready;
  $('[data-act="vscode"]').title = ready ? '' : urlPlaceholder();
}

/**
 * Ask the running VS Code container for VS Code's URL, one request at a time. A
 * failure is shown in the URL box and on the buttons (the VS Code container may still
 * be starting); the next status refresh asks again.
 */
async function fetchVscodeUrl() {
  if (state.vscodeFetching) {
    return;
  }
  state.vscodeFetching = true;
  const epoch = state.epoch;

  let url = '';
  let problem = '';
  try {
    url = await api.invoke('vscode:url');
  } catch (error) {
    problem = errorText(error);
  } finally {
    state.vscodeFetching = false;
  }

  // The URL of the profile shown before a switch: ask again for this one.
  if (epoch !== state.epoch) {
    if (isUp()) {
      fetchVscodeUrl();
    }
    return;
  }

  state.vscodeUrl = url;
  state.vscodeError = problem;

  showVscodeUrl();
  setButtons();
}

/**
 * Give VS Code a new token, once asked: every copy of the old one stops
 * working, and VS Code restarts, so open windows need the new URL.
 */
async function rotateToken() {
  const results = await call('vscode:rotate');
  if (!results) {
    return;
  }

  forgetVscodeUrl();
  state.showToken = false;
  showVscodeUrl();
  toast('New token: VS Code restarted with it', 'ok');
  refreshStatus();
}

/** The dialog for VS Code's port, with the current one to change. */
function openPortDialog() {
  $('#pm-port').value = state.data.vscodePort;
  $('#pm-error').textContent = '';
  $('#port-modal').classList.remove('hidden');
  $('#pm-port').focus();
}

/** Move VS Code to the port the dialog names; what the CLI says if it can't. */
async function savePort() {
  const port = Number($('#pm-port').value);

  try {
    state.data = await api.invoke('vscode:port', port);
  } catch (error) {
    $('#pm-error').textContent = errorText(error);
    return;
  }

  $('#port-modal').classList.add('hidden');
  forgetVscodeUrl();
  render();
  toast(`VS Code on port ${port}`, 'ok');
  refreshStatus();
}

$('#pm-save').onclick = savePort;
$('#pm-cancel').onclick = () => $('#port-modal').classList.add('hidden');
$('#pm-port').onkeydown = (event) => {
  if (event.key === 'Enter') {
    savePort();
  }
  if (event.key === 'Escape') {
    $('#port-modal').classList.add('hidden');
  }
};

// ---------------------------------------------------------------------------
// The dialog that turns a device of this computer on
// ---------------------------------------------------------------------------

/**
 * Open the dialog that turns *device* on: what it gives code in the sandbox,
 * in the CLI's words, and a box to tick that this is understood, without
 * which it can't be confirmed.
 */
function openDeviceDialog(device) {
  state.deviceDialog = device.key;

  $('#dm-title').textContent = `Turn ${device.name} on for profile ${state.data.profile}?`;
  $('#dm-summary').textContent = `It gives the VS Code container ${device.summary}. A running one is recreated with it.`;
  $('#dm-warning').textContent = device.warning;
  $('#dm-understand').checked = false;
  $('#dm-confirm').disabled = true;
  $('#dm-confirm').textContent = `Turn ${device.name} on`;
  $('#dm-error').textContent = '';

  $('#device-modal').classList.remove('hidden');
}

/** Close the device dialog, with nothing turned on. */
function closeDeviceDialog() {
  state.deviceDialog = null;
  $('#device-modal').classList.add('hidden');
}

/**
 * Turn the dialog's device on, once its box is ticked; what the CLI says is
 * shown in the dialog when it refuses (the computer doesn't have it).
 */
async function confirmDevice() {
  const key = state.deviceDialog;
  if (!key || !$('#dm-understand').checked) {
    return;
  }

  $('#dm-confirm').disabled = true;
  try {
    state.data = await api.invoke('device:set', key, true);
  } catch (error) {
    $('#dm-error').textContent = errorText(error);
    $('#dm-confirm').disabled = !$('#dm-understand').checked;
    return;
  }

  closeDeviceDialog();
  render();
  reportOutcome(DEVICE_OUTCOME);
  refreshStatus();
}

$('#dm-understand').onchange = () => {
  $('#dm-confirm').disabled = !$('#dm-understand').checked;
};
$('#dm-confirm').onclick = confirmDevice;
$('#dm-cancel').onclick = closeDeviceDialog;
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && state.deviceDialog) {
    closeDeviceDialog();
  }
});

/** Open VS Code in the browser, at the URL the VS Code container gave. */
function openVscode() {
  if (state.vscodeUrl) {
    call('vscode:open', state.vscodeUrl);
  }
}

// ---------------------------------------------------------------------------
// A container's logs, in a dialog that keeps itself up to date
// ---------------------------------------------------------------------------

/** Open the logs dialog on *service*'s log. */
async function openLogs(service) {
  closeLogs();
  state.logsService = service;

  $('#logs-title').textContent = `Logs: ${service}`;
  $('#logs-out').textContent = '';
  $('#logs-status').textContent = 'loading…';
  $('#logs-modal').classList.remove('hidden');

  await refreshLogs();
}

/** Close the logs dialog, and stop reading the log. */
function closeLogs() {
  clearTimeout(state.logsTimer);
  state.logsTimer = null;
  state.logsService = null;
  $('#logs-modal').classList.add('hidden');
}

/** Read the container's log again, then schedule the next read while the dialog is open. */
async function refreshLogs() {
  const service = state.logsService;
  if (!service) {
    return;
  }
  clearTimeout(state.logsTimer);

  const output = $('#logs-out');
  // Follow the end of the log, unless someone has scrolled up to read.
  const atEnd = output.scrollTop + output.clientHeight >= output.scrollHeight - 4;

  try {
    const text = await api.invoke('stack:logs', service, LOG_LINES);

    // Closed, or another container opened meanwhile.
    if (state.logsService !== service) {
      return;
    }

    output.textContent = text || '(nothing logged yet)';
    $('#logs-status').textContent = `last ${LOG_LINES} lines · ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    if (state.logsService !== service) {
      return;
    }
    $('#logs-status').textContent = errorText(error);
  }

  if (atEnd) {
    output.scrollTop = output.scrollHeight;
  }
  state.logsTimer = setTimeout(refreshLogs, LOG_REFRESH);
}

$('#logs-refresh').onclick = () => refreshLogs();
$('#logs-close').onclick = closeLogs;

// A click outside the dialog, or Escape, closes it.
$('#logs-modal').onclick = (event) => {
  if (event.target === $('#logs-modal')) {
    closeLogs();
  }
};
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && state.logsService) {
    closeLogs();
  }
});

// ---------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------

/** Put the current view on the page, and wire up its controls. */
function render() {
  if (!state.data) {
    return;
  }

  $('#view-title').textContent = TITLES[state.view];
  showWeakened();
  document.querySelectorAll('.nav-item').forEach((button) => {
    button.classList.toggle('active', button.dataset.view === state.view);
  });

  const views = { overview, langs, image: imageView, extensions, allowlist, blocked, forwards, mounts };
  $('#view').innerHTML = views[state.view]();
  bind();
}

/**
 * Render again without taking the focus from the box being typed in.
 * Rendering replaces the box; the new one gets the focus, and the caret
 * where it was.
 */
function renderKeepingFocus() {
  const focused = document.activeElement;
  const id = focused && focused.id && $('#view').contains(focused) ? focused.id : '';
  const caret = id ? focused.selectionStart : null;

  render();

  const replaced = id ? document.getElementById(id) : null;
  if (!replaced) {
    return;
  }

  replaced.focus();
  // Boxes without a caret (number boxes) say null.
  if (typeof caret === 'number') {
    replaced.setSelectionRange(caret, caret);
  }
}

/** Show *view*, and ask for what it shows beyond the profile's state. */
function showView(view) {
  state.view = view;
  render();
  fetchForView();
}

/**
 * Ask for what the view on the page shows beyond the profile's state: the
 * blocked traffic, how the VS Code container's extensions are doing, the
 * profile's own Dockerfile steps.
 */
function fetchForView() {
  if (state.view === 'blocked') {
    actions['refresh-blocked']();
  }

  if (state.view === 'allowlist') {
    actions['refresh-lan']();
  }

  if (state.view === 'extensions') {
    fetchExtensionStatus();
  }

  if (state.view === 'image') {
    fetchImageSteps();
  }
}

/** The "this profile / all profiles" chips, on views whose edits can apply to every profile. */
function scopeChips(what) {
  if (profileCount() < 2) {
    return '';
  }

  const active = (scope) => (state.scope === scope ? 'active' : '');
  return `<div class="chips scope" title="What ${esc(what)} apply to">
    <button class="chip ${active('profile')}" data-scope="profile">this profile</button>
    <button class="chip ${active('all')}" data-scope="all">all ${esc(profileCount())} profiles</button>
  </div>`;
}

/** The callout that says edits apply to every profile, on views with the scope chips. */
function scopeNote(what) {
  if (state.scope !== 'all') {
    return '';
  }

  return `<div class="callout">Scope: <b>all ${esc(profileCount())} profiles</b>. ${esc(what)}; the view shows <span class="mono">${esc(state.data.profile)}</span>.</div>`;
}

// -- the Overview -------------------------------------------------------------

/** A status dot's colour: running and healthy, running but not healthy, exited. */
/**
 * A container's dot: green while it runs and is healthy, amber while it
 * starts, restarts or is unhealthy, red when it stopped while the rest of
 * the sandbox runs, grey when everything is stopped (or it is switched off).
 */
function serviceDot(service) {
  if (service.state === 'running') {
    return service.health && service.health !== 'healthy' ? 'warn' : 'ok';
  }
  if (service.state === 'restarting') {
    return 'warn';
  }

  const othersRun = state.status.services.some((other) => other.state === 'running');
  return service.state === 'exited' && othersRun ? 'bad' : '';
}

/**
 * What a container's row says beside its dot: nothing while all is well
 * (the dot says it, and the tooltip has the details), the reason when it
 * needs a look.
 */
function serviceNote(service) {
  if (service.state === 'running' && (!service.health || service.health === 'healthy')) {
    return '';
  }
  if (service.state === 'running') {
    return service.health;
  }
  return service.status || service.state;
}

// The order the Overview lists the containers in: the one you work in, the
// Docker daemon beside it, then the plumbing that keeps them in.
const SERVICE_ORDER = ['vscode', 'docker', 'squid', 'coredns', 'gateway'];

/** A container's place in the Overview's list; any other goes last. */
function serviceRank(name) {
  const rank = SERVICE_ORDER.indexOf(name);
  return rank === -1 ? SERVICE_ORDER.length : rank;
}

/**
 * One container's row: its dot, its name, a note when it needs a look, and
 * for the Docker daemon its on/off switch. A click opens its logs, except
 * on the switch.
 */
function serviceRow(service, busy) {
  const isDocker = service.service === 'docker';
  const note = isDocker && !state.data.docker ? 'off' : serviceNote(service);
  const exists = Boolean(service.status);
  const title = exists ? `${service.service}: ${service.status} · click for its logs` : `${service.service}: ${note}`;

  const dockerHelp =
    'The rootless Docker daemon beside the VS Code container, which docker, docker compose and Testcontainers talk to. ' +
    'Off: no daemon runs for this profile.';
  const toggle = isDocker
    ? `<label class="switch" title="${esc(dockerHelp)}"><input type="checkbox" data-docker ${state.data.docker ? 'checked' : ''} ${busy}><span></span></label>`
    : '';

  const opens = exists ? ` clickable" data-logs="${esc(service.service)}` : '';
  return (
    `<div class="service-row${opens}" title="${esc(title)}">` +
    `<span class="dot ${isDocker && !state.data.docker ? '' : serviceDot(service)}"></span>` +
    `<span class="mono">${esc(service.service)}</span>` +
    `<span class="service-end"><span class="muted small">${esc(note)}</span>${toggle}</span></div>`
  );
}

/**
 * What the overview's container list says when there are no containers to
 * list. The Docker snap's refusal is said as it is, and the docs' address it
 * ends with ("…another way: <address>") becomes a link on a line of its own.
 */
function emptyServiceList() {
  const error = state.status.error;

  if (DOCKER_SNAP.test(error)) {
    const docs = error.match(DOCS_LINK);
    if (!docs) {
      return `<div class="empty">${esc(error)}</div>`;
    }

    const words = error.slice(0, docs.index).trim().replace(/:$/, '.');
    const link = `<a href="#" data-docs="${esc(docs[0])}">How to install Docker</a>`;
    return `<div class="empty">${esc(words)}<br>${link}</div>`;
  }

  if (error) {
    return `<div class="empty">Docker can't say what is running: ${esc(error)}</div>`;
  }

  return `<div class="empty">Not created yet. Press <b>Start</b> to build and run this profile's sandbox.</div>`;
}

/**
 * The Overview: the profile's containers beside VS Code and the devices of
 * this computer it may use, what it is built around, and counts for the rest.
 */
function overview() {
  return `
    <div class="overview">
      <div class="grid cols-2">${profileCard()}<div class="overview-side">${vscodeCard()}${devicesCard()}</div></div>
      ${languagesCard()}
      <div class="grid cols-4">${extensionsCount()}${otherCounts()}</div>
    </div>`;
}

/** The Overview's profile card: the containers, the profile's buttons and the Docker daemon switch. */
function profileCard() {
  const data = state.data;
  const busy = state.running ? 'disabled' : '';

  const services = state.status.services
    .filter((service) => !SETUP_SERVICES.includes(service.service))
    .sort((first, second) => serviceRank(first.service) - serviceRank(second.service));

  // The Docker daemon's row is there with its switch whether or not its
  // container is: switched off, or not started yet.
  const rows = services.map((service) => serviceRow(service, busy));
  if (!services.some((service) => service.service === 'docker')) {
    const notRunning = { service: 'docker', state: state.data.docker ? 'not running' : 'off', status: '', health: '' };
    const dockerRow = serviceRow(notRunning, busy);
    rows.splice(services.length && services[0].service === 'vscode' ? 1 : 0, 0, dockerRow);
  }
  const serviceList = (services.length ? '' : emptyServiceList()) + rows.join('');

  // Any profile can go; after the last one, the app asks for a new first one.
  const deleteHelp = 'Delete this profile entirely: its containers, volumes, image and files';

  return `
      <div class="card">
        <h2>Profile <span class="mono">${esc(data.profile)}</span></h2>
        <div class="service-list">${serviceList}</div>
        <div class="row-buttons">
          <button class="btn small" data-act="restart" ${busy}>Restart</button>
          <button class="btn small" data-act="rebuild" ${busy} title="build the images, then up -d">Rebuild images</button>
          <button class="btn small danger" data-act="reset" ${busy} title="down -v: removes containers and volumes, keeps the profile">Reset…</button>
          <button class="btn small danger" data-act="delete-profile" ${state.running ? 'disabled' : ''} title="${esc(deleteHelp)}">Delete profile…</button>
        </div>
      </div>`;
}

/** The Overview's VS Code card: where it runs, its URL with the token, and the button that opens it. */
function vscodeCard() {
  const ready = !!state.vscodeUrl; // VS Code can be opened once its URL is known

  return `
      <div class="card accent">
        <h2>VS Code</h2>
        <p class="muted small">Runs in your browser on <code>localhost:${esc(state.data.vscodePort)}</code> (<button class="link" id="change-port" title="Move VS Code to another port on this machine">change</button>), protected by a token kept in the home volume.</p>
        <div class="url-box"><input type="text" class="mono small" id="vscode-url" readonly placeholder="${esc(urlPlaceholder())}"><button class="btn small" id="show-token" title="Show or hide the token" ${ready ? '' : 'disabled'}>${state.showToken ? 'Hide' : 'Show'}</button><button class="btn small" id="copy-url" ${ready ? '' : 'disabled'}>Copy</button></div>
        <div class="row-buttons"><button class="btn small primary" data-act="vscode" ${ready ? '' : 'disabled'}>Open VS Code ↗</button><button class="btn small" id="new-token" title="A new token: every copy of this one stops working, and VS Code restarts" ${ready && !state.running ? '' : 'disabled'}>New token…</button></div>
      </div>`;
}

/**
 * The Overview's Devices card: a switch for each device of this computer the
 * VS Code container may be given, off unless the profile turned it on. One
 * this computer doesn't have can't be switched on, and says why; one that is
 * on can always be switched off. Switching one on asks first (the device
 * dialog).
 */
function devicesCard() {
  const busy = state.running ? 'disabled' : '';
  const rows = state.data.devices.map((device) => deviceRow(device, busy)).join('');

  return `
      <div class="card" id="devices-card">
        <div class="card-head"><h2>Devices</h2><span class="muted small">off unless you need one: each weakens the sandbox</span></div>
        <div class="device-list">${rows}</div>
      </div>`;
}

/** One device's row on the Devices card: its name, what it gives (or why this computer can't), and its switch. */
function deviceRow(device, busy) {
  const locked = !device.available && !device.enabled;
  const note = device.available ? device.summary : `Not available here: ${device.reason}`;

  let help = `Give the VS Code container ${device.summary}`;
  if (device.enabled) {
    help = `Take ${device.name} away from the VS Code container`;
  } else if (locked) {
    help = `Not available here: ${device.reason}`;
  }

  return (
    `<div class="device-row${locked ? ' unavailable' : ''}">` +
    `<div class="device-text"><b>${esc(device.name)}</b> <span class="mono muted small">${esc(device.key)}</span><div class="muted small">${esc(note)}</div></div>` +
    `<label class="switch" title="${esc(help)}"><input type="checkbox" data-device="${esc(device.key)}" ${device.enabled ? 'checked' : ''} ${locked ? 'disabled' : busy}><span></span></label></div>`
  );
}

/** The Overview's Languages / Tools card: what the profile is built around, and whether its image is behind. */
function languagesCard() {
  const on = state.data.langs.filter((lang) => lang.enabled);
  const languages = on.filter((lang) => lang.kind === 'language');
  const tools = on.filter((lang) => lang.kind === 'tool');

  const none = languages.length ? '' : '<span class="muted small">No languages yet.</span>';
  const chips = [
    ...languages.map((lang) => `<span class="lang-chip" title="${esc(lang.desc)}">${esc(lang.name)}</span>`),
    ...tools.map((lang) => `<span class="lang-chip tool" title="${esc(lang.desc)}">${esc(lang.name)}</span>`),
  ];

  const domains = on.flatMap((lang) => lang.domains).length;
  const extensions = on.flatMap((lang) => lang.extensions).length;
  const summary =
    `They open ${counted(domains, 'allowlist entry', 'allowlist entries')} ` +
    `and bring ${counted(extensions, 'VS Code extension', 'VS Code extensions')}.`;

  return `
      <div class="card">
        <div class="card-head"><h2>Languages / Tools</h2><button class="btn small" data-goto="langs">Choose languages and tools…</button></div>
        <div class="tags">${none}${chips.join('')}</div>
        <div class="muted small" style="margin-top:10px">${esc(summary)}${esc(imageNote())}</div>
        ${rebuildCallout('rebuild-image')}
      </div>`;
}

/** The Overview's count of VS Code extensions, and where they come from. */
function extensionsCount() {
  const enabled = state.data.extensions.filter((row) => row.enabled);

  // An extension of the profile's own that a language or every VS Code container
  // already brings is counted once, with them.
  const provided = enabled.filter((row) => row.source !== 'custom');
  const providedIds = new Set(provided.map((row) => row.id));
  const core = provided.filter((row) => row.source === 'core').length;
  const fromLanguages = provided.length - core;
  const own = enabled.filter((row) => row.source === 'custom' && !providedIds.has(row.id)).length;

  return `
      <div class="card"><div class="muted small">VS Code extensions</div><div class="stat">${esc(core + fromLanguages + own)}</div><div class="muted small">${esc(core)} in every VS Code container · ${esc(fromLanguages)} from languages · ${esc(own)} of your own</div></div>`;
}

/** The Overview's counts of allowlist entries, port forwards and mounts. */
function otherCounts() {
  const data = state.data;

  const enabled = data.allowlist.filter((entry) => entry.enabled);
  const readWrite = enabled.filter((entry) => entry.flag === 'rw').length;
  const ssh = enabled.filter((entry) => entry.flag === 'ssh').length;
  const disabled = data.allowlist.filter((entry) => !entry.enabled).length;

  const forwardList = data.forwards.map((forward) => `, ${forward.port}→${forward.host}:${forward.target}`).join('');
  const mountList = data.mounts.length
    ? data.mounts.map((mount) => mount.remote).join(', ')
    : 'none; the home volume holds your work';

  return `
      <div class="card"><div class="muted small">Allowlist entries</div><div class="stat">${esc(enabled.length)}</div><div class="muted small">${esc(readWrite)} read-write · ${esc(ssh)} ssh · ${esc(disabled)} disabled</div></div>
      <div class="card"><div class="muted small">Port forwards</div><div class="stat">${esc(data.forwards.length + 1)}</div><div class="muted small">VS Code${esc(forwardList)}</div></div>
      <div class="card"><div class="muted small">Host folders mounted</div><div class="stat">${esc(data.mounts.length)}</div><div class="muted small">${esc(mountList)}</div></div>`;
}

// -- the image's languages, tools, packages and Dockerfile steps ------------------

/**
 * How the image differs from the profile: "Java and jq not in it yet; Go
 * still in it; your Dockerfile steps changed". Empty when it doesn't, or
 * that can't be told.
 */
function imageDifference() {
  const data = state.data;
  const missing = data.langs.filter((lang) => lang.enabled && lang.inImage === false).map((lang) => lang.name);
  const extra = data.langs.filter((lang) => !lang.enabled && lang.inImage === true).map((lang) => lang.name);

  // The profile's own packages, when the image says which it has.
  const built = data.image.built === true && data.image.packages ? data.image.packages : null;
  if (built) {
    missing.push(...data.packages.filter((name) => !built.includes(name)));
    extra.push(...built.filter((name) => !data.packages.includes(name)));
  }

  const parts = [];
  if (missing.length) {
    parts.push(`${joinNames(missing)} not in it yet`);
  }
  if (extra.length) {
    parts.push(`${joinNames(extra)} still in it`);
  }
  if ((data.rebuildReasons || []).includes('dockerfile')) {
    parts.push('your Dockerfile steps changed');
  }

  return parts.join('; ');
}

/** What the image has, said after the languages and tools: built with them, or to be at the first Start. */
function imageNote() {
  const image = state.data.image;

  if (image.built === false) {
    return ' The image is built with them at the first Start.';
  }
  if (image.built && state.data.rebuildNeeded === false) {
    return ' The image has them.';
  }
  return '';
}

/**
 * The warning that the image was built with other languages, tools,
 * packages or Dockerfile steps than the profile's, with the button that
 * rebuilds it; nothing when it wasn't. *action* is the button's: this
 * profile's rebuild, or the scope's.
 */
function rebuildCallout(action) {
  if (!state.data.rebuildNeeded) {
    return '';
  }

  const busy = state.running ? 'disabled' : '';
  const label = action === 'apply-langs' && state.scope === 'all' ? 'Rebuild all images' : 'Rebuild image';
  const text = `The image isn't what the profile says: ${imageDifference()}. Rebuilding it installs what is missing and recreates the VS Code container (several minutes).`;

  return `<div class="callout row"><span>${esc(text)}</span><button class="btn small primary" data-act="${action}" ${busy}>${esc(label)}</button></div>`;
}

/** A language's or tool's mark where its switch and its image disagree; only once there is an image. */
function imageBadge(lang) {
  if (state.data.image.built !== true) {
    return '';
  }

  if (lang.enabled && lang.inImage === false) {
    return ' <span class="badge warn" title="Rebuild the image to install it">not in the image yet</span>';
  }
  if (!lang.enabled && lang.inImage === true) {
    return ' <span class="badge" title="Rebuild the image to remove it">still in the image</span>';
  }
  return '';
}

// -- Languages / Tools ----------------------------------------------------------

/**
 * The Languages / Tools view: what the profile is built around. A card per
 * language, then per tool, each with its switch, its toolchain or CLI, the
 * allowlist entries it opens and the extensions it brings; then the
 * profile's own Ubuntu packages.
 */
function langs() {
  const data = state.data;
  const all = state.scope === 'all';
  const busy = state.running ? 'disabled' : '';

  const languages = data.langs.filter((lang) => lang.kind === 'language');
  const tools = data.langs.filter((lang) => lang.kind === 'tool');
  const languagesOn = languages.filter((lang) => lang.enabled).length;
  const toolsOn = tools.filter((lang) => lang.enabled).length;

  // The rebuild button is in the warning when there is one, else in the toolbar.
  const rebuildButton = data.rebuildNeeded
    ? ''
    : `<button class="btn" data-act="apply-langs" ${busy} title="Build the VS Code image with these languages and tools and recreate the VS Code container">Rebuild ${all ? 'all images' : 'image'}</button>`;

  const noImage = data.image.built === false ? ' No image yet: it is built with these at the first Start.' : '';

  return `
    <div class="toolbar">
      <span class="muted">${esc(languagesOn)} of ${esc(languages.length)} languages and ${esc(toolsOn)} of ${esc(tools.length)} tools on</span>
      <span class="spacer"></span>
      ${scopeChips('switches and the rebuild')}
      ${rebuildButton}
    </div>
    ${scopeNote("Switching below switches the language or tool in every profile, and the rebuild rebuilds every profile's image")}
    <div class="callout info">Switching a language or tool on or off opens or closes its allowlist entries (a running proxy reloads with them a few seconds after your last change), and a running VS Code container installs or removes its extensions in the background within seconds (reload the VS Code window if one doesn't appear). Its toolchain or CLI comes with the next image rebuild.${esc(noImage)}</div>
    ${rebuildCallout('apply-langs')}
    <h3 class="group-title">Languages</h3>
    <div class="lang-grid">${languages.map(langCard).join('')}</div>
    <h3 class="group-title">Tools</h3>
    <div class="lang-grid">${tools.map(langCard).join('')}</div>
    ${packagesCard()}`;
}

/** One language's or tool's card: its switch, its toolchain, what it opens and what it brings. */
function langCard(lang) {
  const nothing = '<span class="muted small">nothing</span>';
  const opens = lang.domains.map((domain) => domainTag(lang, domain)).join('');
  const brings = lang.extensions.map((id) => `<span class="tag" title="${esc(id)}">${esc(id)}</span>`).join('');
  const switchTitle = `${lang.enabled ? 'Switch off' : 'Switch on'} ${lang.name}${state.scope === 'all' ? ' in every profile' : ''}`;

  return `
      <div class="card lang-card ${lang.enabled ? '' : 'off'}">
        <div class="lang-head">
          <div><b>${esc(lang.name)}</b>${imageBadge(lang)}</div>
          <label class="switch" title="${esc(switchTitle)}"><input type="checkbox" data-lang="${esc(lang.key)}" ${lang.enabled ? 'checked' : ''}><span></span></label>
        </div>
        <div class="desc">${esc(lang.desc)}</div>
        <div class="lang-row"><span class="lang-label">Opens</span><div class="tags">${opens || nothing}</div></div>
        <div class="lang-row"><span class="lang-label">Brings</span><div class="tags">${brings || nothing}</div></div>
      </div>`;
}

/**
 * One of the allowlist entries a language opens; struck through when it is
 * switched off in the allowlist. A long one is cut short, so the tooltip
 * has it whole.
 */
function domainTag(lang, domain) {
  const entry = state.data.allowlist.find((item) => item.language === lang.key && item.entry === domain);

  if (entry && !entry.enabled) {
    return `<span class="tag off" title="${esc(domain)} (switched off in the allowlist)">${esc(domain)}</span>`;
  }
  return `<span class="tag" title="${esc(domain)}">${esc(domain)}</span>`;
}

/**
 * The Ubuntu packages card: the profile's own packages, each with its ✕,
 * and the box that adds more (several at once, separated by spaces). The
 * image installs them, so a change marks it as needing a rebuild, with the
 * view's Rebuild button, as a language switch does.
 */
function packagesCard() {
  const all = state.scope === 'all';
  const chips = state.data.packages.map(packageChip).join('');
  const none = '<span class="muted small">None yet.</span>';

  return `
    <div class="card" id="packages-card" style="margin-top:14px">
      <div class="card-head"><h2>Ubuntu packages</h2><span class="muted small">this profile's own, in VSCODE_PACKAGES in its .env</span></div>
      <p class="muted small">Installed in the image from Ubuntu 26.04's archive, beside what the languages and tools bring: they arrive with the next image rebuild. A name the archive doesn't have fails the rebuild, and the console says so.</p>
      <div class="tags">${chips || none}</div>
      <div class="form-row">
        <div class="field grow"><label>Package</label><input type="text" class="mono" id="pkg-name" placeholder="its name in Ubuntu's archive: postgresql-client" value="${esc(state.packageDraft)}"></div>
        <button class="btn primary" data-act="add-package">Add${all ? ' to all' : ''}</button>
      </div>
      <div class="form-error" id="pkg-error"></div>
    </div>`;
}

/** One of the profile's own packages, with the ✕ that removes it. */
function packageChip(name) {
  const everywhere = state.scope === 'all' ? ' from every profile' : '';
  const removeTitle = `Remove${everywhere}; the image loses it at its next rebuild`;

  return (
    `<span class="package-chip">${esc(name)}` +
    `<button class="btn ghost small icon" data-remove-package="${esc(name)}" title="${esc(removeTitle)}">✕</button></span>`
  );
}

// -- the Image: the profile's own Dockerfile steps ---------------------------------

/**
 * The Image view: the profile's own Dockerfile steps in a plain editor,
 * with Save and Save & rebuild (the rebuild streams to the console), and
 * HardCode's Dockerfile they are built on, to read. The steps run on this
 * computer's Docker, which the view says first. A profile without the file
 * gets the starter in the editor, written when it is saved.
 */
function imageView() {
  const steps = state.imageSteps;
  if (!steps) {
    const why = state.imageStepsError
      ? `The Dockerfile steps couldn't be read: ${state.imageStepsError}`
      : 'Reading the Dockerfile steps…';
    return `<div class="empty">${esc(why)}</div>`;
  }

  const busy = state.running ? 'disabled' : '';
  const saved = steps.exists ? steps.dockerfile : steps.starter;
  const text = state.imageDraft === null ? saved : state.imageDraft;
  const where = steps.exists ? steps.path : `${steps.path} (not there yet: Save writes it)`;

  return `
    <div class="callout">These steps run on your computer's Docker, with your computer's network, not the sandbox's allowlist.</div>
    ${rebuildCallout('rebuild-image')}
    <div class="card">
      <div class="card-head"><h2>Your Dockerfile steps</h2><span class="muted small mono">${esc(where)}</span></div>
      <p class="muted small">Built on HardCode's image, <span class="mono">FROM hardcode</span>, which has the profile's languages, tools and packages. The files they COPY go in the same folder. Without this file the image is HardCode's as it is.</p>
      <textarea id="img-dockerfile" class="dockerfile-editor" spellcheck="false" wrap="off">${esc(text)}</textarea>
      <div class="form-error" id="img-error"></div>
      <div class="row-buttons">
        <button class="btn" data-act="save-image" ${busy}>Save</button>
        <button class="btn primary" data-act="save-image-rebuild" ${busy} title="Save, rebuild the VS Code image with these steps, and recreate the VS Code container">Save &amp; rebuild</button>
      </div>
    </div>
    <div class="card" style="margin-top:14px">
      <div class="card-head"><h2>HardCode's Dockerfile</h2><span class="muted small">what yours builds on; read-only</span></div>
      <pre class="dockerfile-view" id="img-base">${esc(steps.base)}</pre>
    </div>`;
}

// -- Extensions -----------------------------------------------------------------

/**
 * The Extensions view: every VS Code extension the VS Code container gets,
 * grouped by what brings it (every VS Code container, each language or tool
 * that is on, the profile itself), how far the running container has got
 * with them, and the form for the profile's own.
 */
function extensions() {
  const data = state.data;
  const all = state.scope === 'all';
  const rows = data.extensions;

  const core = rows.filter((row) => row.source === 'core');
  const custom = rows.filter((row) => row.source === 'custom');
  const languagesOn = data.langs.filter((lang) => lang.enabled && lang.extensions.length);
  const languagesOff = data.langs.filter((lang) => !lang.enabled && lang.extensions.length);

  const groups = [extensionGroup('Every VS Code container', core, 'Every VS Code container has it')];
  for (const lang of languagesOn) {
    const own = rows.filter((row) => row.source === lang.key);
    groups.push(extensionGroup(lang.name, own, `Comes with ${lang.name}: switch it off in Languages / Tools to remove it`));
  }

  // The languages that are off, and that switching one on brings its own.
  let offNote = '';
  if (languagesOff.length) {
    const names = joinNames(languagesOff.map((lang) => lang.name));
    const their = languagesOff.length === 1 ? 'its' : 'their';
    offNote = `<p class="muted small" style="margin:12px 0 0">Switch on ${esc(names)} in <button class="link" data-goto="langs">Languages / Tools</button> to get ${their} extensions.</p>`;
  }

  const customRows = custom.map(customExtensionRow).join('');
  const noCustom = '<div class="empty">None yet. Add one by its id, as the Marketplace shows it.</div>';

  const wanted = new Set(rows.filter((row) => row.enabled).map((row) => row.id)).size;

  return `
    <div class="toolbar">
      <span class="muted">${esc(counted(wanted, 'extension', 'extensions'))} for this profile's VS Code container${esc(extensionsDone())}</span>
      <span class="spacer"></span>
      ${scopeChips('extension changes')}
    </div>
    ${scopeNote('Adding and removing below changes the same extension in every profile')}
    ${extensionProgress()}
    <div class="callout info">Only extensions whose publisher has a verified domain are installed. A running VS Code container installs and removes them in the background, within seconds of a change: adding or removing one of your own, or switching a language or tool. A stopped one does it when it starts.</div>
    <div class="ext-grid">${groups.join('')}</div>
    ${offNote}
    <div class="card" style="margin-top:14px">
      <div class="card-head"><h2>Your extensions</h2><span class="muted small">this profile's own, in VSCODE_EXTENSIONS in its .env</span></div>
      <div class="form-row">
        <div class="field grow"><label>Extension</label><input type="text" class="mono" id="ext-id" placeholder="publisher.name, as the Marketplace shows it" value="${esc(state.extensionDraft)}"></div>
        <button class="btn primary" data-act="add-extension">Add${all ? ' to all' : ''}</button>
      </div>
      <div class="form-error" id="ext-error"></div>
      <div class="ext-list">${customRows || noCustom}</div>
    </div>`;
}

/**
 * The line above the Extensions view's groups while the running VS Code
 * container installs extensions, or why it couldn't be asked; nothing
 * otherwise.
 */
function extensionProgress() {
  if (state.extensionStatusError) {
    return `<p class="muted small">The VS Code container couldn't be asked how its extensions are doing: ${esc(state.extensionStatusError)}</p>`;
  }

  const status = state.extensionStatus;
  if (!status || status.state !== 'installing') {
    return '';
  }

  const text =
    `Installing in the background: ${status.installed} of ${status.total} in place. ` +
    `VS Code picks them up as they arrive (reload the window if one doesn't appear).`;
  return `<div class="callout info ext-progress"><span class="spin"></span>${esc(text)}</div>`;
}

/**
 * What the toolbar adds once the running VS Code container is done with
 * the extensions: " · all 12 installed", or " · 11 of 12 installed · 1
 * refused"; nothing while it isn't running or is still installing.
 */
function extensionsDone() {
  const status = state.extensionStatus;
  if (!status || status.state !== 'done') {
    return '';
  }

  let words = ` · ${status.installed} of ${status.total} installed`;
  if (status.installed === status.total) {
    words = ` · all ${status.total} installed`;
  }
  if (status.refused.length) {
    words += ` · ${status.refused.length} refused`;
  }
  if (status.failed.length) {
    words += ` · ${status.failed.length} to try again`;
  }

  return words;
}

/** A card listing the extensions that come from the same place; *why* is each one's tooltip. */
function extensionGroup(title, rows, why) {
  const list = rows
    .map(
      (row) =>
        `<div class="ext-row" title="${esc(why)}"><span class="mono">${esc(row.id)}</span>${extensionState(row.id)}</div>`,
    )
    .join('');

  return `
      <div class="card ext-group">
        <div class="card-head"><h2>${esc(title)}</h2><span class="muted small">${esc(rows.length)}</span></div>
        <div class="ext-list">${list}</div>
      </div>`;
}

/** One of the profile's own extensions: its id, what else brings it, how the VS Code container is doing with it, and ✕. */
function customExtensionRow(row) {
  const everywhere = state.scope === 'all' ? ' from every profile' : '';
  const removeTitle = `Remove${everywhere}; a running VS Code container uninstalls it in the background`;

  return (
    `<div class="ext-row"><span class="mono">${esc(row.id)}</span>${duplicateBadge(row)}${extensionState(row.id)}` +
    `<button class="btn ghost small icon" data-remove-extension="${esc(row.id)}" title="${esc(removeTitle)}">✕</button></div>`
  );
}

/** What else brings one of the profile's own extensions: a language, or every VS Code container. */
function duplicateBadge(row) {
  if (!row.duplicate) {
    return '';
  }

  const provider = state.data.extensions.find((other) => other.id === row.id && other.source !== 'custom');
  const name = provider.source === 'core' ? 'every VS Code container' : langName(provider.source);

  // A language that is off doesn't bring it now, but would if switched on.
  const words = provider.enabled ? `already comes with ${name}` : `also comes with ${name}`;
  return `<span class="badge">${esc(words)}</span>`;
}

/**
 * How the running VS Code container is doing with extension *id*, as a mark
 * after it: still installing, refused, or failed. Nothing for one in place,
 * and nothing while the container isn't running.
 */
function extensionState(id) {
  const status = state.extensionStatus;
  if (!status) {
    return '';
  }

  if (status.refused.includes(id)) {
    return '<span class="badge bad ext-state">refused: publisher has no verified domain</span>';
  }
  if (status.failed.includes(id)) {
    return `<span class="badge warn ext-state">couldn't install; tried again shortly</span>`;
  }
  if (status.pending.includes(id)) {
    return '<span class="badge info ext-state">installing…</span>';
  }
  return '';
}

// -- the allowlist ----------------------------------------------------------------

/** Whether an allowlist entry passes the search box and the filter chips. */
function entryMatches(entry, query, filter) {
  const lower = (text) => (text || '').toLowerCase();
  const language = entry.language ? langName(entry.language) : '';
  const found =
    !query ||
    entry.entry.includes(query) ||
    lower(entry.comment).includes(query) ||
    lower(entry.section).includes(query) ||
    lower(language).includes(query);

  const filters = {
    all: true,
    enabled: entry.enabled,
    disabled: !entry.enabled,
    rw: entry.flag === 'rw',
    ssh: entry.flag === 'ssh',
    path: entry.kind === 'path',
    provided: entry.provided,
    custom: !entry.provided,
  };

  return found && !!filters[filter];
}

/** An entry's access, as a badge: read-only, rw or ssh. */
function accessBadge(flag) {
  if (flag === 'rw') {
    return '<span class="badge rw">rw</span>';
  }
  if (flag === 'ssh') {
    return '<span class="badge ssh">ssh</span>';
  }
  return '<span class="badge ro">read-only</span>';
}

/**
 * The heading row above an allowlist section: where its entries come from
 * (every profile, a language or tool, which says where it is switched, or
 * the profile itself).
 */
function sectionRow(entry) {
  if (entry.provided && entry.language) {
    const name = langName(entry.language);
    const kind = langKind(entry.language);
    return `<tr class="section-row"><td colspan="5">${esc(name)} · comes with the ${esc(kind)}; switch it in <button class="link" data-goto="langs">Languages / Tools</button></td></tr>`;
  }

  if (entry.provided) {
    return `<tr class="section-row"><td colspan="5">${esc(entry.section)} · every profile</td></tr>`;
  }

  return `<tr class="section-row"><td colspan="5">${esc(entry.section || 'Custom')} · this profile's own</td></tr>`;
}

/**
 * The entry being added (state.adding) among the rows to show, where the
 * CLI puts it: after the profile's own, which come first. It is shown
 * whatever the search and the filter say, being what was just typed.
 */
function withAdding(entries) {
  if (!state.adding) {
    return entries;
  }

  const row = {
    ...state.adding,
    id: null,
    kind: '',
    comment: '',
    enabled: true,
    provided: false,
    source: 'custom',
    section: 'Custom',
    adding: true,
  };

  const firstProvided = entries.findIndex((entry) => entry.provided);
  const at = firstProvided === -1 ? entries.length : firstProvided;

  return [...entries.slice(0, at), row, ...entries.slice(at)];
}

/** The row of the entry being added: no switch or ✕ until the CLI has written it. */
function addingRow(entry) {
  return `<tr class="adding">
      <td style="width:48px"></td>
      <td class="entry mono">${esc(entry.entry)}</td>
      <td><span class="badge info">adding…</span> ${accessBadge(entry.flag)}</td>
      <td class="muted small"></td>
      <td class="actions"></td></tr>`;
}

/**
 * The allowlist table's rows, with a heading row where a section starts.
 * An entry HardCode provides can be switched off, but not deleted: it comes
 * with every profile, or with its language. A language's has a way to the
 * Languages / Tools view where the ✕ would be.
 */
function allowlistRows(entries) {
  const removeTitle = `Delete this line${state.scope === 'all' ? ' in every profile' : ''}`;
  let html = '';
  let lastSection = null;

  for (const entry of entries) {
    // A section of the profile's own may share a block's name: where an
    // entry comes from tells them apart.
    const section = `${entry.source}/${entry.section}`;
    if (section !== lastSection) {
      html += sectionRow(entry);
      lastSection = section;
    }

    if (entry.adding) {
      html += addingRow(entry);
      continue;
    }

    const invalid = entry.invalid ? ' <span class="badge" style="color:var(--danger)">invalid</span>' : '';

    // A provided entry has no ✕: a language's names the language, and has a
    // way to Languages / Tools in its place; every profile's section says
    // where it comes from, and its switch is the way to stop allowing it.
    let languageBadge = '';
    let switchTitle = '';
    let action = `<button class="btn ghost small icon" data-remove="${esc(entry.id)}" title="${esc(removeTitle)}">✕</button>`;
    if (entry.provided && entry.language) {
      const name = langName(entry.language);
      const held = `Comes with ${name}: switch it off here, or ${name} off in Languages / Tools`;
      languageBadge = ` <span class="badge lang" title="${esc(held)}">${esc(name)}</span>`;
      action = `<button class="btn ghost small icon" data-goto="langs" title="${esc(held)}">⌘</button>`;
    } else if (entry.provided) {
      switchTitle = 'Comes with every profile: switch it off to stop allowing it';
      action = '';
    }

    html += `<tr class="${entry.enabled ? '' : 'disabled'}">
      <td style="width:48px"><label class="switch" title="${esc(switchTitle)}"><input type="checkbox" data-toggle="${esc(entry.id)}" ${entry.enabled ? 'checked' : ''}><span></span></label></td>
      <td class="entry mono">${esc(entry.entry)}${invalid}</td>
      <td><span class="badge">${esc(entry.kind)}</span> ${accessBadge(entry.flag)}${languageBadge}</td>
      <td class="muted small">${esc(entry.comment)}</td>
      <td class="actions">${action}</td></tr>`;
  }

  return html;
}

/**
 * A name on your network's state, in words: invalid (the proxy won't start
 * with it), still checking, left out (it's this computer), ok, or that it
 * now resolves elsewhere, with the
 * address it could move to (`moved`), when the CLI found one it may reach:
 * never this computer's, nor one in the sandbox's own network.
 */
function lanStatus(pin, checked) {
  if (pin.problem) {
    return { text: `invalid: ${pin.problem}`, moved: '' };
  }
  if (!checked) {
    return { text: 'checking…', moved: '' };
  }
  if (checked.state === 'moved') {
    return { text: `now resolves to ${checked.resolves.join(', ')}`, moved: checked.moveTo || '' };
  }
  if (checked.state === 'unresolved') {
    return { text: "ok (doesn't resolve from here)", moved: '' };
  }
  if (checked.state === 'this-computer') {
    return { text: 'left out: this computer, which a sandbox never reaches', moved: '' };
  }
  return { text: 'ok', moved: '' };
}

/**
 * The On your network card: the names this profile lets reach an address on
 * your own network, which the proxy refuses otherwise, each with its state.
 * One that now resolves elsewhere keeps its address, and offers to move.
 */
function lanSection() {
  const pins = state.data.lan || [];
  const states = state.lanStates || {};

  const rows = pins
    .map((pin) => {
      const status = lanStatus(pin, states[pin.name]);
      const move = status.moved
        ? `<button class="btn small" data-lan-move="${esc(pin.name)}" data-address="${esc(status.moved)}">Move to ${esc(status.moved)}…</button> `
        : '';
      const attention = pin.problem || status.moved ? ' style="color:var(--danger)"' : '';

      return `<tr>
      <td class="entry mono">${esc(pin.name)}</td>
      <td class="mono">${esc(pin.address)}</td>
      <td class="small"${attention}>${esc(status.text)}</td>
      <td class="actions">${move}<button class="btn ghost small icon" data-lan-remove="${esc(pin.name)}" title="Stop letting it reach your network">✕</button></td></tr>`;
    })
    .join('');

  const empty = 'Nothing. When you add a name that points inside your own network, HardCode asks whether to let it reach that address.';

  return `
    <div class="card" id="lan-card" style="margin-bottom:14px">
      <h2>On your network</h2>
      <p class="muted small" style="margin-top:0">The proxy refuses names that point inside your own network. A name listed here may reach the address shown, and nothing else on your network.</p>
      <table><thead><tr><th>Name</th><th>Address</th><th>State</th><th></th></tr></thead>
      <tbody>${rows || `<tr><td colspan="4" class="empty">${esc(empty)}</td></tr>`}</tbody></table>
    </div>`;
}

/** The Allowlist view: the search, the filters, the form, and every entry by section. */
function allowlist() {
  const data = state.data;
  const filter = state.allowlistFilter;
  const query = state.allowlistQuery.toLowerCase();
  const shown = data.allowlist.filter((entry) => entryMatches(entry, query, filter));
  const rows = allowlistRows(withAdding(shown));
  const prefill = state.prefill || {};
  const all = state.scope === 'all';

  const filterChips = Object.entries(ALLOWLIST_FILTERS)
    .map(([chip, label]) => `<button class="chip ${filter === chip ? 'active' : ''}" data-filter="${esc(chip)}">${esc(label)}</button>`)
    .join('');

  const selected = (flag) => (prefill.flag === flag ? 'selected' : '');
  const accessOptions =
    `<option value="" ${!prefill.flag ? 'selected' : ''}>read-only (GET, clone/pull)</option>` +
    `<option value="rw" ${selected('rw')}>read-write (uploads, push)</option>` +
    `<option value="ssh" ${selected('ssh')}>ssh tunnel</option>`;

  return `
    <div class="toolbar">
      <input type="text" class="search" id="al-search" placeholder="Search entries…" value="${esc(state.allowlistQuery)}">
      <div class="chips">${filterChips}</div>
      <span class="spacer"></span>
      ${scopeChips('changes')}
      <button class="btn" data-act="reload-proxy" ${state.running ? 'disabled' : ''} title="Give the running proxy the allowlist now: after editing the file by hand, or rather than wait for the changes below to reach it">Reload ${all ? 'all proxies' : 'proxy'}</button>
    </div>
    ${scopeNote("Adding, toggling and deleting below changes the same entry in every profile's allowlist")}
    <div class="card" style="margin-bottom:14px">
      <div class="form-row">
        <div class="field grow"><label>Entry</label><input type="text" class="mono" id="al-entry" placeholder="example.com  ·  .example.com  ·  github.com/myuser  ·  host:22 (ssh)" value="${esc(prefill.entry || '')}"></div>
        <div class="field"><label>Access</label><select id="al-flag">${accessOptions}</select></div>
        <button class="btn primary" data-act="add-allowlist" ${state.adding ? 'disabled' : ''}>Add${all ? ' to all' : ''}</button>
      </div>
      <div class="form-error" id="al-error"></div>
      <p class="muted small" style="margin:0">Read-only still lets git clone, fetch and pull. A path such as <code>github.com/myuser</code> allows only that namespace. The entries HardCode provides (every profile's, and those of the languages and tools that are on) can be switched off but not deleted: one switched off stays off through updates, and one an update adds arrives switched on. Every change is written to <code>profiles/${esc(data.profile)}/allowlist.txt</code> at once, and a running proxy reloads with it a few seconds after your last change (the header says <i>applying allowlist</i> meanwhile); with the sandbox stopped it applies at the next start. <b>Reload</b> gives it at once, and is for edits you made to the file by hand.</p>
    </div>
    ${lanSection()}
    <div class="card" style="padding:0">
      <table><thead><tr><th></th><th>Entry</th><th>Type / access</th><th>Note</th><th></th></tr></thead><tbody>${rows || '<tr><td colspan="5" class="empty">No entries match.</td></tr>'}</tbody></table>
    </div>`;
}

// -- Blocked traffic, port forwards, mounts ----------------------------------------

// Why the proxy refused, when it said, as Blocked traffic shows it.
const REFUSED_BECAUSE = { 'internal-address': 'resolves to an internal address' };

/**
 * A Blocked traffic row's buttons. A name refused for pointing inside your
 * network gets "Allow on your network", since an entry wouldn't let it
 * through; the rest "Allow host", and "Allow URL" when the URL says more.
 */
function blockedActions(target, index) {
  if (target.reason === 'internal-address') {
    return `<button class="btn small" data-allow-lan="${esc(index)}">Allow on your network</button>`;
  }

  const allowUrl =
    target.url && target.url !== `https://${target.host}/`
      ? `<button class="btn small" data-allow-url="${esc(index)}">Allow URL</button>`
      : '';

  // A space between the buttons, as between any two inline ones.
  return `<button class="btn small" data-allow-host="${esc(index)}">Allow host</button>${allowUrl ? ` ${allowUrl}` : ''}`;
}

/** The Blocked traffic view: what the proxy and the DNS filter refused, why when it said, and buttons to allow it. */
function blocked() {
  const rows = state.blocked
    .map((target, index) => {
      const what = target.kind === 'dns' ? `DNS ${target.method}` : target.method;
      const reason = REFUSED_BECAUSE[target.reason];
      const why = reason ? `<div class="muted small">${esc(reason)}</div>` : '';

      return `<tr>
    <td><span class="badge ${target.kind === 'dns' ? 'dns' : ''}">${esc(what)}</span></td>
    <td class="mono">${esc(target.url || target.target)}${why}</td>
    <td>${esc(target.count)}</td>
    <td class="muted small mono">${esc(target.client)}</td>
    <td class="actions">${blockedActions(target, index)}</td></tr>`;
    })
    .join('');

  const periods = ['10m', '30m', '2h', '24h']
    .map((period) => `<option ${state.blockedSince === period ? 'selected' : ''}>${esc(period)}</option>`)
    .join('');
  const empty = isUp() ? 'Nothing blocked in this period.' : 'The sandbox is not running.';

  return `
    <div class="toolbar">
      <span class="muted">Refused by this profile's proxy and DNS filter in the last</span>
      <select id="blocked-since">${periods}</select>
      <button class="btn small" data-act="refresh-blocked">Refresh</button>
      <span class="spacer"></span>
      <span class="muted small">${esc(state.blocked.length)} distinct targets</span>
    </div>
    <div class="callout info">Each row is something inside the sandbox tried to reach and couldn't. "Allow host" or "Allow URL" pre-fills the allowlist form; nothing is allowed until you add and apply it.</div>
    <div class="card" style="padding:0">
      <table><thead><tr><th>What</th><th>Target</th><th>Count</th><th>From</th><th></th></tr></thead>
      <tbody>${rows || `<tr><td colspan="5" class="empty">${esc(empty)}</td></tr>`}</tbody></table>
    </div>`;
}

/** The Port forwards view: VS Code's forward, the profile's others, and the form. */
function forwards() {
  const data = state.data;

  const vscodeRow =
    `<tr><td class="mono">localhost:${esc(data.vscodePort)}</td><td class="mono">vscode:8000</td>` +
    `<td class="muted small">VS Code (VSCODE_PORT in this profile's .env)</td><td></td></tr>`;

  const forwardRows = data.forwards.map((forward) => {
    const where = forward.host === 'docker' ? 'a container port, published with docker run -p' : 'a server in the VS Code container';
    return (
      `<tr><td class="mono">localhost:${esc(forward.port)}</td><td class="mono">${esc(forward.host)}:${esc(forward.target)}</td>` +
      `<td class="muted small">${esc(where)}</td>` +
      `<td class="actions"><button class="btn ghost small icon" data-remove-forward="${esc(forward.port)}" title="Remove">✕</button></td></tr>`
    );
  });

  const rows = [vscodeRow, ...forwardRows].join('');

  return `
    <div class="card" style="margin-bottom:14px">
      <div class="form-row">
        <div class="field"><label>Host port</label><input type="number" id="fw-port" min="1" max="65535" placeholder="3000" style="width:110px"></div>
        <div class="field"><label>Runs in</label><select id="fw-host"><option value="vscode">the VS Code container</option><option value="docker">a container (docker run -p)</option></select></div>
        <div class="field"><label>Target port</label><input type="number" id="fw-target" min="1" max="65535" placeholder="same" style="width:110px"></div>
        <button class="btn primary" data-act="add-forward">Add forward</button>
      </div>
      <div class="form-error" id="fw-error"></div>
      <p class="muted small" style="margin:0">Written to this profile's <code>compose.override.yaml</code>; the gateway is recreated at once if the sandbox is running. Host ports belong to one profile each. Servers in the VS Code container must listen on <code>0.0.0.0</code>, not localhost.</p>
    </div>
    <div class="card" style="padding:0"><table><thead><tr><th>On this machine</th><th>In the sandbox</th><th></th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

/** The Mounts view: the host folders mounted into the VS Code container, and the form. */
function mounts() {
  const data = state.data;
  const all = state.scope === 'all';

  const rows = data.mounts
    .map((mount) => {
      const access = mount.mode === 'ro' ? '<span class="badge ro">read-only</span>' : '<span class="badge rw">read-write</span>';
      const removeTitle = `Remove${all ? ' from every profile' : ''}`;
      return (
        `<tr><td class="mono">${esc(mount.local)}</td><td class="mono">${esc(mount.remote)}</td><td>${access}</td>` +
        `<td class="actions"><button class="btn ghost small icon" data-remove-mount="${esc(mount.remote)}" title="${esc(removeTitle)}">✕</button></td></tr>`
      );
    })
    .join('');

  const toolbar = profileCount() > 1 ? `<div class="toolbar"><span class="spacer"></span>${scopeChips('mount changes')}</div>` : '';

  return `
    ${toolbar}
    <div class="card" style="margin-bottom:14px">
      <div class="form-row">
        <div class="field grow"><label>Folder on this machine</label><div class="url-box"><input type="text" class="mono" id="mt-local" placeholder="/home/you/Git"><button class="btn" data-act="pick-folder">Browse…</button></div></div>
        <div class="field"><label>Path in the VS Code container</label><input type="text" class="mono" id="mt-remote" placeholder="git  (→ /home/dev/git)" style="width:220px"></div>
        <label class="check"><input type="checkbox" id="mt-ro"> read-only</label>
        <button class="btn primary" data-act="add-mount">Mount${all ? ' in all' : ''}</button>
      </div>
      <div class="form-error" id="mt-error"></div>
      <div class="callout">A writable mount is a way out of the sandbox: anything inside, Claude included, can change files your host later runs (<code>.git/hooks</code>, build scripts). Prefer read-only for repos you only need to read.</div>
      <p class="muted small" style="margin:0">Mounted in the VS Code container and in docker at the same path, so <code>docker run -v</code> works for it. Applying recreates both containers (VS Code reloads).</p>
    </div>
    <div class="card" style="padding:0"><table><thead><tr><th>This machine</th><th>VS Code</th><th>Access</th><th></th></tr></thead><tbody>${rows || '<tr><td colspan="4" class="empty">No host folders mounted in this profile.</td></tr>'}</tbody></table></div>`;
}

// ---------------------------------------------------------------------------
// Wiring the controls of the view on the page
// ---------------------------------------------------------------------------

/** Run *handler* for each element in the view matching *selector*. */
function each(selector, handler) {
  $('#view').querySelectorAll(selector).forEach(handler);
}

/** Open the allowlist view with its form filled in. */
function prefillAllowlist(entry, flag = '') {
  state.prefill = { entry, flag };
  showView('allowlist');
}

/**
 * The entry a refused tunnel's "Allow host" fills in: its host, with ssh and
 * the port for an SSH tunnel (none for 22, the default).
 */
function tunnelEntry(target) {
  if (!target.port) {
    return { entry: target.host, flag: '' };
  }

  const entry = target.port === 22 ? target.host : `${target.host}:${target.port}`;
  return { entry, flag: 'ssh' };
}

/**
 * Offer to let each name the last change found on your network reach its
 * address there (the CLI found them: they resolve inside, and aren't on it
 * yet), one dialog per name. Nothing happens without a yes.
 */
async function offerLan() {
  const results = (state.data && state.data.lastResults) || [];
  const offered = new Set();

  for (const result of results) {
    const found = result.data && result.data.lan;
    if (!found || found.pinned || offered.has(found.name)) {
      continue;
    }

    offered.add(found.name);
    await lanOffer(found.name, found.address);
  }
}

/** Ask whether *name* may reach *address* on your network, and add it if so. */
async function lanOffer(name, address) {
  const answer = await call('lan:offer', name, address, state.scope);
  if (!answer) {
    return;
  }

  state.data = answer;
  render();
  reportOutcome(ALLOWLIST_OUTCOME);
  actions['refresh-lan']();
}

/** The controls every view may have: action buttons, the scope chips, links to other views. */
function bindCommon() {
  each('[data-act]', (button) => {
    button.onclick = () => actions[button.dataset.act](button);
  });

  each('[data-scope]', (chip) => {
    chip.onclick = () => {
      state.scope = chip.dataset.scope;
      render();
    };
  });

  each('[data-goto]', (link) => {
    link.onclick = () => showView(link.dataset.goto);
  });
}

/** The Allowlist view's search box, filter chips, switches, ✕ buttons and form. */
function bindAllowlist() {
  const search = $('#al-search');
  if (search) {
    search.oninput = () => {
      state.allowlistQuery = search.value;
      renderKeepingFocus();
    };
  }

  each('[data-filter]', (chip) => {
    chip.onclick = () => {
      state.allowlistFilter = chip.dataset.filter;
      render();
    };
  });

  each('[data-toggle]', (toggle) => {
    toggle.onchange = async () => {
      state.data = await change('allowlist:setEnabled', +toggle.dataset.toggle, toggle.checked, state.scope);
      render();
      reportOutcome(ALLOWLIST_OUTCOME);
    };
  });

  each('[data-remove]', (button) => {
    button.onclick = async () => {
      state.data = await change('allowlist:remove', +button.dataset.remove, state.scope);
      render();
      reportOutcome(ALLOWLIST_OUTCOME);
    };
  });

  each('[data-lan-remove]', (button) => {
    button.onclick = async () => {
      state.data = await change('lan:remove', button.dataset.lanRemove, state.scope);
      render();
      reportOutcome(ALLOWLIST_OUTCOME);
      actions['refresh-lan']();
    };
  });

  // A name that moved keeps its address until someone says otherwise.
  each('[data-lan-move]', (button) => {
    button.onclick = () => lanOffer(button.dataset.lanMove, button.dataset.address);
  });

  const entry = $('#al-entry');
  if (entry) {
    entry.onkeydown = (event) => {
      if (event.key === 'Enter') {
        actions['add-allowlist']();
      }
    };
  }
}

/** The Blocked traffic view's "Allow" buttons and its period. */
function bindBlocked() {
  each('[data-allow-host]', (button) => {
    button.onclick = () => {
      const { entry, flag } = tunnelEntry(state.blocked[+button.dataset.allowHost]);
      prefillAllowlist(entry, flag);
    };
  });

  // The name is looked up again here, so the question names the address it
  // would reach.
  each('[data-allow-lan]', (button) => {
    button.onclick = async () => {
      const name = state.blocked[+button.dataset.allowLan].host;
      const found = await call('lan:check', name);

      if (found.pinned) {
        toast(`${name} is already on your network, at ${found.pinned}`);
      } else if (found.address) {
        await lanOffer(name, found.address);
      } else if (found.refused) {
        // This computer, or the sandbox's own network: refused, never asked.
        toast(found.refused, 'error');
      } else {
        toast(`${name} doesn't resolve to an address on your network from here`, 'error');
      }
    };
  });

  each('[data-allow-url]', (button) => {
    button.onclick = () => {
      // The URL's directory: allowing the exact file is rarely what is wanted.
      const url = new URL(state.blocked[+button.dataset.allowUrl].url);
      prefillAllowlist(url.host + url.pathname.replace(/\/[^/]*$/, ''));
    };
  });

  const since = $('#blocked-since');
  if (since) {
    since.onchange = () => {
      state.blockedSince = since.value;
      actions['refresh-blocked']();
    };
  }
}

/** The ✕ buttons of the Port forwards and Mounts views. */
function bindForwardsAndMounts() {
  each('[data-remove-forward]', (button) => {
    button.onclick = async () => {
      state.data = await change('forwards:remove', +button.dataset.removeForward);
      render();
      toast('Forward removed', 'ok');
    };
  });

  each('[data-remove-mount]', (button) => {
    button.onclick = async () => {
      state.data = await change('mounts:remove', button.dataset.removeMount, state.scope);
      render();
      toast('Mount removed', 'ok');
    };
  });
}

/**
 * The Languages / Tools view's switches. A switch changes the allowlist
 * and the extension list at once (a running proxy is given the allowlist
 * in the background, a few seconds after the last change), and leaves the
 * image for the rebuild, so the view shows what is now out of date. A
 * running VS Code container's installing is followed from then on.
 */
function bindLanguages() {
  each('[data-lang]', (toggle) => {
    toggle.onchange = async () => {
      const lang = langOf(toggle.dataset.lang);
      const enabled = toggle.checked;
      const head = `${lang.name} ${enabled ? 'on' : 'off'}`;

      state.data = await change('langs:set', lang.key, enabled, state.scope);
      render();
      reportOutcome(languageOutcome(lang, enabled), head);

      if (changeApplied() && lang.extensions.length) {
        watchExtensions();
      }
    };
  });
}

/** The Extensions view's form and ✕ buttons. */
function bindExtensions() {
  const input = $('#ext-id');
  if (input) {
    input.oninput = () => {
      state.extensionDraft = input.value;
    };
    input.onkeydown = (event) => {
      if (event.key === 'Enter') {
        actions['add-extension']();
      }
    };
  }

  each('[data-remove-extension]', (button) => {
    button.onclick = async () => {
      state.data = await change('extensions:remove', button.dataset.removeExtension, state.scope);
      render();
      reportOutcome(EXTENSION_REMOVED);

      if (changeApplied()) {
        watchExtensions();
      }
    };
  });
}

/** The Ubuntu packages card's form and ✕ buttons, on the Languages / Tools view. */
function bindPackages() {
  const input = $('#pkg-name');
  if (input) {
    input.oninput = () => {
      state.packageDraft = input.value;
    };
    input.onkeydown = (event) => {
      if (event.key === 'Enter') {
        actions['add-package']();
      }
    };
  }

  each('[data-remove-package]', (button) => {
    button.onclick = async () => {
      const name = button.dataset.removePackage;
      state.data = await change('packages:remove', name, state.scope);
      render();
      reportOutcome(PACKAGE_OUTCOME, `Removed ${name} · gone from the image at its next rebuild`);
    };
  });
}

/** The Image view's editor: what is typed in it is kept until it is saved. */
function bindImage() {
  const editor = $('#img-dockerfile');
  if (editor) {
    editor.oninput = () => {
      state.imageDraft = editor.value;
    };
  }
}

/** The Overview's Docker daemon and device switches, Copy button and container rows. */
function bindOverview() {
  each('[data-docker]', (toggle) => {
    // The switch sits on the daemon's row, which opens its logs: a click on
    // the switch is the switch's alone.
    toggle.closest('.switch').onclick = (event) => event.stopPropagation();

    toggle.onchange = async () => {
      state.data = await change('docker:set', toggle.checked, 'profile');
      render();
      reportOutcome(DOCKER_OUTCOME);
      refreshStatus();
    };
  });

  // On waits for the dialog's yes, so the switch stays off until then; off
  // happens at once.
  each('[data-device]', (toggle) => {
    toggle.onchange = async () => {
      const device = state.data.devices.find((item) => item.key === toggle.dataset.device);

      if (toggle.checked) {
        toggle.checked = false;
        openDeviceDialog(device);
        return;
      }

      state.data = await change('device:set', device.key, false);
      render();
      reportOutcome(DEVICE_OUTCOME);
      refreshStatus();
    };
  });

  const copy = $('#copy-url');
  if (copy) {
    copy.onclick = () => {
      navigator.clipboard.writeText(state.vscodeUrl);
      toast('URL copied, token included', 'ok');
    };
  }

  const showToken = $('#show-token');
  if (showToken) {
    showToken.onclick = () => {
      state.showToken = !state.showToken;
      showVscodeUrl();
    };
  }

  const newToken = $('#new-token');
  if (newToken) {
    newToken.onclick = rotateToken;
  }

  const changePort = $('#change-port');
  if (changePort) {
    changePort.onclick = openPortDialog;
  }

  each('[data-logs]', (row) => {
    row.onclick = () => openLogs(row.dataset.logs);
  });

  each('[data-docs]', (link) => {
    link.onclick = (event) => {
      event.preventDefault();
      call('docs:open', link.dataset.docs);
    };
  });

  // The URL itself is asked for by refreshStatus, whatever the view.
  showVscodeUrl();
}

/** Wire up the controls of the view on the page. */
function bind() {
  bindCommon();
  bindAllowlist();
  bindBlocked();
  bindForwardsAndMounts();
  bindLanguages();
  bindPackages();
  bindImage();
  bindExtensions();
  bindOverview();
}

// ---------------------------------------------------------------------------
// Actions, by the data-act of the button that starts them
// ---------------------------------------------------------------------------

/** Rebuild the VS Code image with the languages of the profile (or of every profile), streamed to the console. */
async function rebuildImage(scope) {
  state.data = await start('rebuild vscode', 'langs:apply', scope);
  render();
  toast(scope === 'all' ? 'Every image rebuilt' : 'Image rebuilt', 'ok');
}

/**
 * Save what the Image view's editor holds as the profile's Dockerfile
 * steps; whether it was saved. A refusal is said under the editor.
 */
async function saveImageSteps() {
  const text = $('#img-dockerfile').value;

  try {
    state.data = await api.invoke('image:save', text);
  } catch (error) {
    $('#img-error').textContent = errorText(error);
    return false;
  }

  state.imageDraft = null;
  await fetchImageSteps();
  toast('Dockerfile steps saved · built at the next image rebuild', 'ok');
  return true;
}

const actions = {
  async 'add-allowlist'() {
    const entry = $('#al-entry').value.trim();
    const flag = $('#al-flag').value;

    // One at a time: Enter in the entry box gets here with Add disabled too.
    if (state.adding) {
      return;
    }

    // The entry shows at once, among the profile's own, while the CLI
    // writes it (a running proxy is given it in the background afterwards);
    // the list the CLI reads back then takes its place. With nothing typed,
    // the CLI just says what to type.
    if (entry) {
      state.adding = { entry, flag };
      state.prefill = null;
      render();
      toast(addingMessage(entry));
    }

    // A refused entry goes back into the form, with why next to it rather
    // than in a toast.
    try {
      state.data = await api.invoke('allowlist:add', entry, flag, state.scope);
    } catch (error) {
      state.adding = null;
      state.prefill = { entry, flag };
      render();
      $('#al-error').textContent = errorText(error);
      return;
    }

    state.adding = null;
    render();
    reportOutcome(ALLOWLIST_OUTCOME);
    await offerLan();
  },

  async 'refresh-lan'() {
    const epoch = state.epoch;
    const checked = await call('lan:list');

    // Asked for the profile shown before a switch.
    if (epoch !== state.epoch) {
      return;
    }

    state.lanStates = Object.fromEntries(checked.map((item) => [item.name, item]));
    if (state.view === 'allowlist') {
      render();
    }
  },

  async 'reload-proxy'() {
    const applied = await call('allowlist:apply', state.scope);
    const message = applied.length
      ? `Proxy reloaded: ${applied.join(', ')}`
      : 'Nothing running; the allowlist applies at the next start';
    toast(message, 'ok');
    refreshStatus();
  },

  async 'refresh-blocked'() {
    const epoch = state.epoch;
    const blocked = await call('traffic:blocked', state.blockedSince);

    // Asked for the profile shown before a switch.
    if (epoch !== state.epoch) {
      return;
    }

    state.blocked = blocked;
    render();
  },

  async 'add-forward'() {
    const port = +$('#fw-port').value;
    const host = $('#fw-host').value;
    const target = +$('#fw-target').value || port;

    try {
      state.data = await api.invoke('forwards:add', port, host, target);
    } catch (error) {
      $('#fw-error').textContent = errorText(error);
      return;
    }

    render();
    toast(`Forwarding localhost:${port}`, 'ok');
  },

  async 'pick-folder'() {
    const folder = await call('mounts:pickFolder');
    if (folder) {
      $('#mt-local').value = folder;
    }
  },

  async 'add-mount'() {
    const local = $('#mt-local').value.trim();
    const remote = $('#mt-remote').value.trim();
    const readOnly = $('#mt-ro').checked;

    try {
      state.data = await api.invoke('mounts:add', local, remote, readOnly, state.scope);
    } catch (error) {
      $('#mt-error').textContent = errorText(error);
      return;
    }

    render();
    toast('Mount added', 'ok');
  },

  async 'add-extension'() {
    const id = $('#ext-id').value.trim();

    // A refused id is explained next to the form, not in a toast.
    try {
      state.data = await api.invoke('extensions:add', id, state.scope);
    } catch (error) {
      $('#ext-error').textContent = errorText(error);
      return;
    }

    state.extensionDraft = '';
    render();
    reportOutcome(EXTENSION_ADDED);

    if (changeApplied()) {
      watchExtensions();
    }
  },

  async 'add-package'() {
    const names = $('#pkg-name').value.trim().split(/\s+/).filter(Boolean);

    // Checked here first, with the CLI's rule, so that a name that isn't
    // one is said next to the form at once; the CLI checks again.
    const bad = names.find((name) => !PACKAGE_NAME.test(name));
    if (!names.length || bad) {
      $('#pkg-error').textContent = bad
        ? `'${bad}' is not an Ubuntu package name: lower-case letters, digits, +, - and ., at least two characters`
        : "Type a package's name, as Ubuntu's archive has it";
      return;
    }

    try {
      state.data = await api.invoke('packages:add', names, state.scope);
    } catch (error) {
      $('#pkg-error').textContent = errorText(error);
      return;
    }

    state.packageDraft = '';
    render();
    reportOutcome(PACKAGE_OUTCOME, `Added ${names.join(' ')} · installed at the next image rebuild`);
  },

  async 'save-image'() {
    await saveImageSteps();
  },

  // Save, then rebuild the profile's image with the steps, streamed to the console.
  async 'save-image-rebuild'() {
    if (await saveImageSteps()) {
      await rebuildImage('profile');
    }
  },

  // The Languages / Tools view's rebuild: for the profile, or every profile with the "all" scope.
  async 'apply-langs'() {
    await rebuildImage(state.scope);
  },

  // The Overview's rebuild, which is about the profile shown.
  async 'rebuild-image'() {
    await rebuildImage('profile');
  },

  async restart() {
    await start('restart', 'stack:restart');
  },

  async rebuild() {
    await start('rebuild', 'stack:rebuild');
    await reloadAfterBuild();
  },

  async reset() {
    await start('reset', 'stack:reset');
    refreshStatus();
  },

  vscode() {
    openVscode();
  },

  async 'delete-profile'() {
    await start(`delete ${state.data.profile}`, 'profiles:remove', state.data.profile);
    await showHomeProfile();
  },
};

// ---------------------------------------------------------------------------
// Profiles: switching, and the "new profile" dialog
// ---------------------------------------------------------------------------

$('#profile-select').onchange = async () => {
  // From here on, replies for the profile shown until now are dropped.
  forgetProfile();

  state.data = await call('profiles:select', $('#profile-select').value);
  await loadState();
  refreshStatus();
  fetchForView();
};

/** One checkbox of the new-profile dialog's language picker. */
function pickOption(lang) {
  return (
    `<label class="pick" title="${esc(lang.desc)}">` +
    `<input type="checkbox" data-pick value="${esc(lang.key)}"><span>${esc(lang.name)}</span></label>`
  );
}

/** Fill the dialog's picker from the catalogue: the languages, then the tools. */
function fillPicker() {
  const languages = catalogue();

  $('#np-langs').innerHTML = languages.filter((lang) => lang.kind === 'language').map(pickOption).join('');
  $('#np-tools').innerHTML = languages.filter((lang) => lang.kind === 'tool').map(pickOption).join('');

  document.querySelectorAll('#modal [data-pick]').forEach((box) => {
    box.onchange = showPickSummary;
  });
}

/**
 * The keys of the languages and tools ticked in the dialog, in catalogue
 * order; null when there is no picker (no profile could be read), so the
 * new profile keeps its source's.
 */
function pickedLanguages() {
  const boxes = [...document.querySelectorAll('#modal [data-pick]')];
  if (!boxes.length) {
    return null;
  }

  return boxes.filter((box) => box.checked).map((box) => box.value);
}

/** Tick exactly the languages and tools in *keys*. */
function tickLanguages(keys) {
  document.querySelectorAll('#modal [data-pick]').forEach((box) => {
    box.checked = keys.includes(box.value);
  });

  showPickSummary();
}

/** Under the picker: what the ticked languages and tools add up to. */
function showPickSummary() {
  const picked = (pickedLanguages() || []).map(langOf);
  const languages = picked.filter((lang) => lang.kind === 'language').length;
  const tools = picked.length - languages;
  const domains = picked.flatMap((lang) => lang.domains).length;
  const extensions = picked.flatMap((lang) => lang.extensions).length;

  $('#np-summary').textContent =
    `${counted(languages, 'language', 'languages')} and ${counted(tools, 'tool', 'tools')}: ` +
    `they open ${counted(domains, 'allowlist entry', 'allowlist entries')} and bring ` +
    `${counted(extensions, 'VS Code extension', 'VS Code extensions')}; the image is built with them at the first Start.`;
}

/**
 * Tick what the chosen source is built around. The shown profile's
 * languages are known, another profile's are asked for, and the template
 * starts with the tools and no language: choosing them is the first thing
 * a new profile asks for.
 */
async function pickFromSource() {
  const from = $('#np-from').value;
  const request = ++state.pickRequest;
  $('#np-error').textContent = '';

  if (from === '_template') {
    const tools = catalogue().filter((lang) => lang.kind === 'tool');
    tickLanguages(tools.map((lang) => lang.key));
    return;
  }

  if (from === state.data.profile) {
    tickLanguages(enabledKeys(state.data.langs));
    return;
  }

  // Create waits for the answer, so it can't take the last source's ticks.
  $('#np-summary').textContent = `Reading ${from}'s languages…`;
  $('#np-create').disabled = true;

  let languages = null;
  let problem = '';
  try {
    languages = await api.invoke('profiles:languages', from);
  } catch (error) {
    problem = errorText(error);
  }

  // Another source was chosen meanwhile, or the dialog closed: that one counts.
  if (request !== state.pickRequest) {
    return;
  }
  $('#np-create').disabled = false;

  if (problem) {
    $('#np-error').textContent = `Couldn't read ${from}'s languages: ${problem}`;
    showPickSummary();
    return;
  }

  tickLanguages(enabledKeys(languages));
}

/**
 * Open the new-profile dialog: a name, what to start from, and its
 * languages, ticked as the source has them. The *first* profile of a home
 * starts as "default", from the template, and the dialog can't be
 * dismissed: there is nothing to show without a profile.
 */
function openNewProfile(first = false) {
  const known = state.data ? state.data.profiles : [];
  const sources = known.map((profile) => {
    const selected = state.data && profile.name === state.data.profile ? 'selected' : '';
    return `<option value="${esc(profile.name)}" ${selected}>copy of ${esc(profile.name)}</option>`;
  });
  $('#np-from').innerHTML = `<option value="_template">the template (defaults)</option>` + sources.join('');

  $('#np-title').textContent = first ? 'Your first profile' : 'New profile';
  $('#np-name').value = first ? 'default' : '';
  $('#np-cancel').hidden = first;
  $('#np-error').textContent = '';
  $('#np-create').disabled = false;
  state.firstProfile = first;
  fillPicker();
  pickFromSource();

  $('#modal').classList.remove('hidden');
  $('#np-name').focus();
}

/** Close the new-profile dialog; a language lookup still on its way is dropped. */
function closeNewProfile() {
  if (state.firstProfile) {
    return;
  }

  state.pickRequest += 1;
  $('#modal').classList.add('hidden');
}

// Not the handler itself: the click event would be taken for *first*.
$('#btn-new-profile').onclick = () => openNewProfile();
$('#np-cancel').onclick = closeNewProfile;
$('#np-from').onchange = pickFromSource;

$('#np-name').onkeydown = (event) => {
  if (event.key === 'Enter') {
    $('#np-create').click();
  }
  if (event.key === 'Escape') {
    closeNewProfile();
  }
};

$('#np-create').onclick = async () => {
  const name = $('#np-name').value.trim().toLowerCase();
  const from = $('#np-from').value;
  const languages = pickedLanguages();

  try {
    state.data = await api.invoke('profiles:create', name, from, languages);
  } catch (error) {
    $('#np-error').textContent = errorText(error);
    return;
  }

  state.firstProfile = false;
  closeNewProfile();

  // A new profile opens on its languages and tools: what it is built around comes first.
  state.view = 'langs';
  await loadState();
  refreshStatus();
  toast(`Profile ${name} created (VS Code on port ${state.data.vscodePort}). Its image is built at the first Start.`, 'ok');
};

// ---------------------------------------------------------------------------
// The navigation and the header
// ---------------------------------------------------------------------------

document.querySelectorAll('.nav-item').forEach((button) => {
  button.onclick = () => showView(button.dataset.view);
});

$('#btn-start').onclick = async () => {
  await start('start', 'stack:start');
  // The first start builds the image, with the profile's languages.
  await reloadAfterBuild();
};
$('#btn-stop').onclick = () => start('stop', 'stack:stop');
$('#btn-vscode').onclick = openVscode;

$('#btn-home').onclick = () => call('home:open');

// ---------------------------------------------------------------------------
// Smoke mode (`--smoke`, for CI and tests/app.py): render every view once,
// check what each must show, try the language, extension, package, image
// and device channels against the real CLI, report what went wrong, and exit
// ---------------------------------------------------------------------------

/** Wait until *condition* holds, for at most *milliseconds*; whether it did. */
async function waitUntil(condition, milliseconds) {
  const deadline = Date.now() + milliseconds;

  while (!condition()) {
    if (Date.now() > deadline) {
      return false;
    }
    await sleep(100);
  }

  return true;
}

/**
 * What a view must show once it is open: the Languages / Tools view its
 * name, a card for every language and tool and the Ubuntu packages card
 * with its form, the Extensions view its groups and its form.
 */
function checkView(view, errors) {
  if (view === 'langs') {
    const label = $('.nav-item[data-view="langs"]').textContent;
    if (!label.endsWith('Languages / Tools') || $('#view-title').textContent !== 'Languages / Tools') {
      errors.push(`the languages view is called "${label}" in the sidebar and "${$('#view-title').textContent}" on top`);
    }

    const cards = $('#view').querySelectorAll('.lang-card').length;
    if (cards !== state.data.langs.length) {
      errors.push(`the Languages / Tools view shows ${cards} cards for ${state.data.langs.length} languages and tools`);
    }

    if (!$('#packages-card') || !$('#pkg-name')) {
      errors.push('the Languages / Tools view has no Ubuntu packages card with its form');
    }
  }

  if (view === 'allowlist' && !$('#lan-card')) {
    errors.push('the Allowlist view has no On your network card');
  }

  if (view === 'extensions') {
    const groups = $('#view').querySelectorAll('.ext-group').length;
    if (!groups || !$('#ext-id')) {
      errors.push(`the Extensions view shows ${groups} extension groups, ${$('#ext-id') ? 'with' : 'without'} its form`);
    }
  }
}

/**
 * The Extensions view with the VS Code container's answer in, through the
 * bridge and the CLI for real. The smoke run has no Docker daemon, so
 * nothing runs: the status says null (in stack:status too), and the view
 * shows its groups and its form, with no installing line and no marks on
 * the rows.
 */
async function checkExtensionStatus(errors) {
  if (state.status.extensions !== null) {
    errors.push(`stack:status says the extensions are ${JSON.stringify(state.status.extensions)}, with nothing running`);
  }

  showView('extensions');
  if (!(await waitUntil(() => state.extensionStatusAsked, 15000))) {
    errors.push('extensions:status never answered');
    return;
  }
  if (state.extensionStatusError) {
    errors.push(`extensions:status: ${state.extensionStatusError}`);
    return;
  }
  if (state.extensionStatus !== null) {
    errors.push(`extensions:status says ${JSON.stringify(state.extensionStatus)}, with nothing running`);
  }

  checkView('extensions', errors);
  if ($('#view').querySelector('.ext-progress, .ext-state')) {
    errors.push('the Extensions view shows extensions installing, with nothing running');
  }
}

/**
 * The language and extension channels, through the bridge and the CLI for
 * real, without changing the profile: a profile's languages (this one's),
 * and adding an extension every VS Code container already has, which the
 * CLI declines.
 */
async function checkLanguageChannels(errors) {
  try {
    const languages = await api.invoke('profiles:languages', state.data.profile);
    if (languages.length !== state.data.langs.length) {
      errors.push(`profiles:languages gave ${languages.length} languages and tools, not ${state.data.langs.length}`);
    }
  } catch (error) {
    errors.push(`profiles:languages: ${errorText(error)}`);
  }

  const core = state.data.extensions.find((row) => row.source === 'core');
  try {
    const answer = await api.invoke('extensions:add', core.id, 'profile');
    const result = answer.lastResults[0];
    if (result.changed || !result.message.includes('already comes with')) {
      errors.push(`extensions:add ${core.id}: ${result.message}`);
    }
  } catch (error) {
    errors.push(`extensions:add ${core.id}: ${errorText(error)}`);
  }
}

/**
 * The names on your network, through the bridge and the CLI for real: the
 * smoke run's profile has none, so lan:list answers an empty list, and the
 * Allowlist view says there is nothing there.
 */
async function checkLan(errors) {
  try {
    const checked = await api.invoke('lan:list');
    if (!Array.isArray(checked) || checked.length) {
      errors.push(`lan:list gave ${JSON.stringify(checked)}, with nothing on your network`);
    }
  } catch (error) {
    errors.push(`lan:list: ${errorText(error)}`);
  }

  showView('allowlist');
  await sleep(200);
  if (!$('#lan-card .empty')) {
    errors.push('the On your network card does not say that nothing is there');
  }
}

/**
 * The allowlist form's Add, through the bridge and the CLI for real: the
 * entry shows at once, marked as being added, with Add disabled; then as
 * the CLI wrote it, among the profile's own, which come before the blocks
 * HardCode provides.
 */
async function checkAllowlistAdd(errors) {
  showView('allowlist');
  await sleep(200);

  $('#al-entry').value = 'smoke.example';
  const adding = actions['add-allowlist']();

  const row = $('tr.adding');
  if (!row || !row.textContent.includes('smoke.example')) {
    errors.push('an allowlist entry being added is not shown at once');
  }
  if (!$('[data-act="add-allowlist"]').disabled) {
    errors.push('Add is not disabled while an allowlist entry is being added');
  }

  await adding;

  const entries = state.data.allowlist;
  const added = entries.findIndex((entry) => entry.entry === 'smoke.example' && !entry.provided);
  const firstProvided = entries.findIndex((entry) => entry.provided);
  if (added === -1) {
    errors.push(`the allowlist entry added is not in the list: ${$('#al-error').textContent}`);
  } else if (firstProvided !== -1 && added > firstProvided) {
    errors.push("the profile's own allowlist entries are not listed first");
  }
  if ($('tr.adding')) {
    errors.push('the allowlist entry added is still marked as being added');
  }
}

/**
 * The Ubuntu packages card, through the bridge and the CLI for real: a
 * name that isn't one is refused next to the form without a call; one
 * that is shows as a chip once the CLI wrote it, marked for the rebuild
 * in the profile's state; its ✕ takes it away again.
 */
async function checkPackages(errors) {
  showView('langs');
  await sleep(200);

  $('#pkg-name').value = 'Not_A_Package';
  await actions['add-package']();
  if (!$('#pkg-error').textContent.includes('not an Ubuntu package name') || state.data.packages.length) {
    errors.push(`a bad package name: "${$('#pkg-error').textContent}", packages ${JSON.stringify(state.data.packages)}`);
  }

  $('#pkg-name').value = 'smoke-package';
  await actions['add-package']();
  const chip = $('#packages-card [data-remove-package="smoke-package"]');
  if (!chip || state.data.packages.join(' ') !== 'smoke-package') {
    errors.push(`packages:add smoke-package: ${$('#pkg-error').textContent || JSON.stringify(state.data.packages)}`);
    return;
  }

  await chip.onclick();
  if (state.data.packages.length || $('#packages-card [data-remove-package]')) {
    errors.push(`packages:remove smoke-package left ${JSON.stringify(state.data.packages)}`);
  }
}

/**
 * The Image view, through the bridge and the CLI for real: the warning
 * that the steps run on this computer, the profile's Dockerfile in the
 * editor (a new profile's is the starter, FROM hardcode), HardCode's to
 * read, and a Save that writes it back.
 */
async function checkImage(errors) {
  showView('image');
  if (!(await waitUntil(() => state.imageSteps || state.imageStepsError, 15000))) {
    errors.push('image:get never answered');
    return;
  }
  if (state.imageStepsError) {
    errors.push(`image:get: ${state.imageStepsError}`);
    return;
  }

  const warning = $('#view .callout');
  if (!warning || !warning.textContent.includes("your computer's Docker")) {
    errors.push('the Image view does not say that the steps run on your computer');
  }

  const editor = $('#img-dockerfile');
  if (!editor || !/^FROM hardcode$/m.test(editor.value)) {
    errors.push(`the Image view's editor holds: ${editor ? editor.value.slice(0, 80) : 'no editor'}`);
    return;
  }
  if (!$('#img-base') || !/^FROM ubuntu/m.test($('#img-base').textContent)) {
    errors.push("the Image view does not show HardCode's Dockerfile");
  }

  if (!(await saveImageSteps()) || !state.imageSteps.exists || state.imageSteps.dockerfile !== editor.value) {
    errors.push(`image:save: ${$('#img-error') ? $('#img-error').textContent : 'the view went away'}`);
  }
}

/**
 * The Devices card and its dialog, through the bridge and the CLI for real,
 * giving the sandbox nothing: a switch per device showing the profile's
 * state, one this computer doesn't have locked with why; the header's mark
 * only while one is on; the dialog's warning, its confirm locked until the
 * box is ticked, and its Cancel; an off that is so already, which the CLI
 * declines; and an on this computer can't give, which the CLI refuses
 * whatever the page says.
 */
async function checkDevices(errors) {
  const listed = state.data.devices;
  if (!Array.isArray(listed) || listed.map((device) => device.key).join(',') !== 'kvm,gpu,nvidia') {
    errors.push(`the profile's devices are ${JSON.stringify(listed)}`);
    return;
  }

  for (const device of listed) {
    const toggle = $(`#devices-card [data-device="${device.key}"]`);
    if (!toggle) {
      errors.push(`the Devices card has no switch for ${device.key}`);
      continue;
    }

    const locked = !device.available && !device.enabled;
    if (toggle.checked !== device.enabled || toggle.disabled !== locked) {
      errors.push(`the ${device.key} switch is checked ${toggle.checked} and disabled ${toggle.disabled}, for ${JSON.stringify(device)}`);
    }
    if (locked && !$('#devices-card').textContent.includes(device.reason)) {
      errors.push(`the Devices card does not say why ${device.key} isn't on this computer`);
    }
  }

  const on = listed.filter((device) => device.enabled);
  if ($('#weakened').hidden !== !on.length) {
    errors.push(`the header's weakened mark is ${$('#weakened').hidden ? 'hidden' : 'shown'} with ${on.length} devices on`);
  }

  openDeviceDialog(listed[0]);
  if ($('#device-modal').classList.contains('hidden') || $('#dm-warning').textContent !== listed[0].warning) {
    errors.push("the device dialog does not open with the device's warning");
  }
  if (!$('#dm-confirm').disabled) {
    errors.push('the device dialog can be confirmed before its box is ticked');
  }
  $('#dm-understand').click();
  if ($('#dm-confirm').disabled) {
    errors.push("ticking the device dialog's box does not let it be confirmed");
  }
  $('#dm-cancel').click();
  if (!$('#device-modal').classList.contains('hidden') || state.deviceDialog) {
    errors.push('Cancel does not close the device dialog');
  }

  try {
    const result = (await api.invoke('device:set', 'kvm', false)).lastResults[0];
    if (result.changed || result.message !== 'device kvm is already off') {
      errors.push(`device:set kvm off: ${result.message}`);
    }
  } catch (error) {
    errors.push(`device:set kvm off: ${errorText(error)}`);
  }

  const lacking = listed.find((device) => !device.available);
  if (lacking) {
    try {
      await api.invoke('device:set', lacking.key, true);
      errors.push(`device:set ${lacking.key} on was not refused, on a computer without it`);
    } catch (error) {
      if (!errorText(error).includes("can't be given to the VS Code container here")) {
        errors.push(`device:set ${lacking.key} on: ${errorText(error)}`);
      }
    }
  }
}

/** Whether *element* is on the screen: shown, not merely in the page. */
function displayed(element) {
  return element.getClientRects().length > 0;
}

/**
 * The new-profile dialog, as ＋ opens it with profiles there: "New profile"
 * with no name, a box for every language and tool, the template ticking
 * only the tools, and a Cancel that closes it.
 */
async function checkNewProfile(errors) {
  $('#btn-new-profile').click();
  await sleep(200);

  if ($('#np-title').textContent !== 'New profile' || $('#np-name').value !== '') {
    errors.push(`＋ opens "${$('#np-title').textContent}" named "${$('#np-name').value}", not a new profile`);
  }

  const boxes = document.querySelectorAll('#modal [data-pick]').length;
  if (boxes !== state.data.langs.length) {
    errors.push(`the new-profile dialog offers ${boxes} languages and tools, not ${state.data.langs.length}`);
  }

  $('#np-from').value = '_template';
  $('#np-from').dispatchEvent(new Event('change'));
  await sleep(100);

  const tools = state.data.langs.filter((lang) => lang.kind === 'tool').map((lang) => lang.key);
  const ticked = pickedLanguages() || [];
  if (ticked.join(',') !== tools.join(',')) {
    errors.push(`the template ticks ${ticked.join(',') || 'nothing'}, not the tools`);
  }

  if (!displayed($('#np-cancel'))) {
    errors.push('the new-profile dialog has no Cancel');
  }
  $('#np-cancel').click();
  if (!$('#modal').classList.contains('hidden')) {
    errors.push('Cancel does not close the new-profile dialog');
  }
}

/** Before the first profile: the dialog that makes it is open, as "default", with the tools ticked. */
function checkFirstProfile(errors) {
  if ($('#modal').classList.contains('hidden')) {
    errors.push('no profile, and no dialog to make the first one');
    return;
  }

  if ($('#np-name').value !== 'default' || displayed($('#np-cancel'))) {
    errors.push('the first profile is not offered as "default", or the dialog shows a Cancel');
  }

  const offered = document.querySelectorAll('#modal [data-pick]').length;
  if (offered !== (state.catalogue || []).length || !offered) {
    errors.push(`the first-profile dialog offers ${offered} languages and tools`);
  }

  const tools = catalogue().filter((lang) => lang.kind === 'tool').map((lang) => lang.key);
  if ((pickedLanguages() || []).join(',') !== tools.join(',')) {
    errors.push('the first profile does not start with the tools ticked');
  }
}

/**
 * Then make it, as someone new does: Create, with what the dialog offers.
 * It must open the new profile, with no error in the dialog.
 */
async function createFirstProfile(errors) {
  await $('#np-create').onclick();

  const shown = $('#np-error').textContent;
  if (shown) {
    errors.push(`creating the first profile failed: ${shown}`);
    return;
  }

  if (!state.data || state.data.profile !== 'default' || !$('#modal').classList.contains('hidden')) {
    errors.push('creating the first profile did not open it');
  }
}

/**
 * The start-up check for a newer release answered, and the page shows what
 * it said: an offer with its version, or nothing (a development build, or
 * no newer release). The smoke run reports the version it was offered.
 */
async function checkUpdate(errors) {
  const error = await state.updateCheck;
  if (error) {
    errors.push(`the check for a newer release failed: ${error}`);
  }

  const shown = !$('#update').hidden;
  if (shown !== Boolean(state.update)) {
    errors.push(`the newer release is ${state.update ? 'not ' : ''}offered, and update:check said ${JSON.stringify(state.update)}`);
  }
  if (state.update && !$('#update-version').textContent.endsWith(state.update.version)) {
    errors.push(`the offer says "${$('#update-version').textContent}" for ${state.update.version}`);
  }
}

/** The sidebar says which HardCode this is: its command's version, as the CLI gave it. */
function checkVersion(errors) {
  const shown = $('#app-version').textContent;
  if (!/^\d+\.\d+\.\d+/.test(shown)) {
    errors.push(`the sidebar's version is "${shown}"`);
  }
}

/** Render every view, check them, report to the app (which exits), and tell it how many views there were. */
async function smoke() {
  const errors = [];
  window.addEventListener('error', (event) => errors.push(event.message));
  window.addEventListener('unhandledrejection', (event) => {
    if (!(event.reason && event.reason.toasted)) {
      errors.push(String(event.reason));
    }
  });

  // Before the first profile there is only the dialog that makes it: no
  // views. It is checked, then used.
  if (!state.data) {
    await sleep(300);
    checkFirstProfile(errors);
    checkVersion(errors);
    await checkUpdate(errors);
    await createFirstProfile(errors);
    api.invoke('smoke:done', 0, errors, state.update && state.update.version);
    return;
  }

  // The security state the page shows must come from the CLI, not from a
  // default. Checked here, on the Overview, before the views are cycled.
  if (typeof state.data.docker !== 'boolean') {
    errors.push(`docker state is ${typeof state.data.docker}, not a boolean`);
  }
  const dockerSwitch = $('[data-docker]');
  if (!dockerSwitch) {
    errors.push('no Docker daemon switch on the Overview');
  } else if (dockerSwitch.checked !== state.data.docker) {
    errors.push('the Docker daemon switch does not show the profile state');
  }

  // So must the devices of this computer it may use, beside it.
  await checkDevices(errors);

  // The logs dialog, through the bridge and the CLI for real (the smoke
  // run's profile has no containers, so the log is empty, not an error).
  await openLogs('vscode');
  if ($('#logs-modal').classList.contains('hidden')) {
    errors.push('the logs dialog did not open');
  } else if (!$('#logs-status').textContent.startsWith('last ')) {
    errors.push(`the logs dialog says: ${$('#logs-status').textContent}`);
  }
  closeLogs();

  for (const button of document.querySelectorAll('.nav-item')) {
    button.click();
    await sleep(300);
    checkView(button.dataset.view, errors);
  }

  await checkExtensionStatus(errors);
  await checkLanguageChannels(errors);
  await checkLan(errors);
  await checkPackages(errors);
  await checkImage(errors);
  await checkAllowlistAdd(errors);
  await checkNewProfile(errors);
  checkVersion(errors);
  await checkUpdate(errors);

  api.invoke('smoke:done', document.querySelectorAll('.nav-item').length, errors, state.update && state.update.version);
}

// ---------------------------------------------------------------------------
// The home: where the profiles are, and the first one
// ---------------------------------------------------------------------------

/** Show where the CLI keeps the profiles, under the navigation; a long path may break after any slash. */
function showHome(home) {
  $('#home-path').innerHTML = esc(home || '').replace(/[/\\]/g, '$&<wbr>');
  $('#home-path').title = home || '';
}

/** Show which HardCode this is under its name: its bundled command's version, "0.1.0 (dev, 2c419ae)" for a development build. */
function showVersion(version) {
  $('#app-version').textContent = version || '';
}

/**
 * Show the profile the home says to (the one last used, or another), or,
 * with none left, ask for the first: its languages and tools are the one
 * thing a profile needs.
 */
async function showHomeProfile() {
  let where;
  try {
    where = await call('home:get');
  } catch {
    return; // shown; the CLI can't be run
  }
  showHome(where.home);
  showVersion(where.version);

  if (!where.profile) {
    forgetProfile();
    state.data = null;
    state.catalogue = where.catalogue;
    $('#view').innerHTML = '';
    $('#profile-select').innerHTML = '';
    showWeakened();
    openNewProfile(true);
    return;
  }

  try {
    await loadState();
  } catch {
    // Already shown; the profile list is the way out.
  }
  await refreshStatus();
}

// ---------------------------------------------------------------------------
// Updates: a release asks the project's releases for a newer one as it
// starts, and offers it under the navigation
// ---------------------------------------------------------------------------

/**
 * Ask whether there is a newer release, and offer it. Quiet when that can't
 * be answered (offline, GitHub down): the app works the same, and asks again
 * at its next start. A development build is never offered one. Resolves to
 * the error, if there was one.
 */
async function checkForUpdate() {
  try {
    state.update = await api.invoke('update:check');
  } catch (error) {
    console.warn(`couldn't check for a newer release: ${errorText(error)}`);
    return errorText(error);
  }

  showUpdate();
  return '';
}

/** The newer release, under the navigation, or nothing. */
function showUpdate() {
  const update = state.update;

  $('#update').hidden = !update;
  if (!update) {
    return;
  }

  $('#update-version').textContent = `HardCode ${update.version}`;
  $('#update').title = `This is HardCode ${update.current}`;
  $('#btn-update-notes').hidden = !/^https:\/\//.test(update.notes);
}

/**
 * Install the newer release and start it, which closes this window; on a
 * failure (shown), the button is there to try again.
 */
async function installUpdate() {
  const button = $('#btn-update');
  button.disabled = true;
  button.textContent = 'Installing…';

  try {
    await call('update:install');
  } catch {
    button.disabled = false;
    button.textContent = 'Install and restart';
  }
}

$('#btn-update').onclick = installUpdate;
$('#btn-update-notes').onclick = () => call('update:notes', state.update.notes);

// ---------------------------------------------------------------------------
// Start-up
// ---------------------------------------------------------------------------

(async () => {
  // The status is polled whatever happens next: a profile that fails to load
  // (shown as a toast) mustn't leave the page without one.
  scheduleStatus();
  state.updateCheck = checkForUpdate();

  await showHomeProfile();

  if (await api.invoke('smoke:mode').catch(() => false)) {
    smoke();
  }
})();
