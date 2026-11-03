/**
 * The admin UI's page.
 *
 * It talks to the admin API (web.py) with fetch, and shows four views: the
 * profiles, one profile (its languages and tools first, then its
 * extensions, allowlist, blocked traffic, access and settings), every
 * profile's allowlist at once, and the admins. Each view is a function
 * returning its HTML and the buttons for the header; `render()` puts the
 * current one on the page and `bind()` wires up its controls.
 *
 * A profile is built around languages and tools: each opens its block of
 * the allowlist and brings its VS Code extensions, which the running
 * VS Code container installs in the background. The catalogue of them
 * comes from GET /api/languages once, at start-up.
 */
'use strict';

const $ = (selector) => document.querySelector(selector);

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

/** Text made safe to put into HTML, attributes included. */
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ESCAPES[character]);

// What the page is showing, and what it knows.
const state = {
  me: null,
  view: 'profiles',
  profiles: [],
  profile: null,
  showToken: false, // whether the Access tab shows the VS Code token, or hides it
  tab: 'languages',
  // Every language and tool a profile can have: [{key, name, desc, kind, domains, extensions}].
  catalogue: [],
  blocked: [],
  since: '30m',
  users: [],
  query: '',
  bulkEntry: '',
  bulkFlag: '',
  bulkResult: '',
  prefill: '', // the allowlist form's entry, from Blocked traffic
  prefillFlag: '', // and its access: ssh for a refused SSH tunnel
  lanPrefill: { name: '', address: '' }, // the On your network form, from Blocked traffic or a moved name
  // The On your network card's last Check: the profile it was for, and each
  // name's {address, resolves, state} then. The ten-second refresh never
  // looks names up, so this is kept apart from the profile it replaces.
  lanChecks: { profile: '', pins: {} },
  // The ten-second refresh's interval, while signed in.
  poller: null,
};

const TITLES = { profiles: 'Profiles', bulk: 'All allowlists', users: 'Admins', profile: 'Profile' };
const TAB_TITLES = {
  languages: 'Languages / Tools',
  extensions: 'Extensions',
  allowlist: 'Allowlist',
  blocked: 'Blocked traffic',
  access: 'Access',
  settings: 'Settings',
};

// Debian's rule for a package's name, which the API checks too (hardcode_cli/packages.py):
// lower-case letters, digits, +, - and ., at least two characters, starting with a letter or digit.
const PACKAGE_NAME = /^[a-z0-9][a-z0-9+.-]+$/;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** A message in the corner that goes away by itself; errors stay longer. */
function toast(message, kind = '') {
  const element = document.createElement('div');
  element.className = `toast ${kind}`;
  element.textContent = message;
  $('#toasts').appendChild(element);

  setTimeout(() => element.remove(), kind === 'error' ? 7000 : 3500);
}

/**
 * Call the admin API and return its JSON answer. A 401 outside signing in
 * means the session ended: back to the sign-in form. A failure is thrown as
 * an Error with the server's message and the response's `status`.
 *
 * `options.poll` marks the page's background refresh, which the server
 * doesn't count as activity: an open tab nobody uses still times out.
 */
async function api(method, path, body, options = {}) {
  // Every write carries the JSON content type: the server refuses writes
  // without it, which keeps plain cross-site forms out.
  const headers = method === 'GET' ? {} : { 'Content-Type': 'application/json' };
  if (options.poll) {
    headers['X-HardCode-Poll'] = '1';
  }

  const response = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: 'same-origin',
  });
  const data = response.status === 204 ? null : await response.json().catch(() => null);

  const signingIn = path.startsWith('/api/login') || path.startsWith('/api/totp');
  if (response.status === 401 && !signingIn) {
    showLogin();
    throw new Error('signed out');
  }

  if (!response.ok) {
    const error = new Error((data && data.error) || `${method} ${path}: ${response.status}`);
    error.status = response.status;
    throw error;
  }

  return data;
}

/** The API, with a failure shown as a toast (and still thrown, so the caller stops). */
async function call(method, path, body, options = {}) {
  try {
    return await api(method, path, body, options);
  } catch (error) {
    toast(error.message, 'error');
    throw error;
  }
}

const profilePath = (suffix = '') => `/api/profiles/${state.profile.name}${suffix}`;

/** A language's or tool's name from the catalogue; its key when the catalogue doesn't have it. */
function languageName(key) {
  const lang = state.catalogue.find((entry) => entry.key === key);
  return lang ? lang.name : key;
}

/** Whether *key* is a language or a tool, in words: "language" or "tool". */
function languageKind(key) {
  const lang = state.catalogue.find((entry) => entry.key === key);
  return lang ? lang.kind : 'language';
}

// ---------------------------------------------------------------------------
// Signing in: a password, then a TOTP code (or, the first time, enrolment)
// ---------------------------------------------------------------------------

/**
 * The sign-in form, at its password step. The refresh stops: it would only
 * get 401s, and each one would put this form back to its first step.
 */
function showLogin() {
  clearInterval(state.poller);
  state.poller = null;

  $('#shell').classList.add('hidden');
  $('#login').classList.remove('hidden');
  $('#login-step-password').classList.remove('hidden');
  $('#login-step-enrol').classList.add('hidden');
  $('#login-step-totp').classList.add('hidden');
  $('#lg-email').focus();
}

function showApp() {
  $('#login').classList.add('hidden');
  $('#shell').classList.remove('hidden');
}

$('#lg-submit').onclick = async () => {
  $('#lg-error').textContent = '';

  let answer;
  try {
    answer = await api('POST', '/api/login', { email: $('#lg-email').value.trim(), password: $('#lg-password').value });
  } catch (error) {
    $('#lg-error').textContent = error.message;
    return;
  }

  $('#login-step-password').classList.add('hidden');

  if (answer.next === 'enrol') {
    $('#lg-qr').src = answer.qr;
    $('#lg-secret').textContent = `Manual entry: ${answer.secret}`;
    $('#login-step-enrol').classList.remove('hidden');
  }

  $('#login-step-totp').classList.remove('hidden');
  $('#lg-code').value = '';
  $('#lg-code').focus();
};

$('#lg-password').onkeydown = (event) => {
  if (event.key === 'Enter') $('#lg-submit').click();
};

$('#lg-verify').onclick = async () => {
  $('#lg-code-error').textContent = '';

  try {
    await api('POST', '/api/totp', { code: $('#lg-code').value.trim() });
  } catch (error) {
    // "wrong code" (web.py's second_factor) is the one 401 that leaves the
    // challenge open. Any other means it is over: it timed out after five
    // minutes ("sign in first"), took too many wrong codes, or was revoked.
    // A code can't help then; the password step comes back.
    const challengeOver = error.status === 401 && error.message !== 'wrong code';
    if (challengeOver) {
      const timedOut = error.message === 'sign in first';
      showLogin();
      $('#lg-error').textContent = timedOut ? 'the sign-in timed out; enter your password again' : error.message;
      return;
    }

    $('#lg-code-error').textContent = error.message;
    return;
  }

  await boot();
};

$('#lg-code').onkeydown = (event) => {
  if (event.key === 'Enter') $('#lg-verify').click();
};

$('#btn-logout').onclick = async () => {
  await api('POST', '/api/logout', {});
  showLogin();
};

// ---------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------

function render() {
  $('#view-title').textContent = TITLES[state.view];
  $('#crumb').textContent = state.view === 'profile' && state.profile ? `/ ${state.profile.name}` : '';

  // One profile's view belongs under Profiles in the navigation.
  document.querySelectorAll('.nav-item').forEach((button) => {
    const current = button.dataset.view === state.view || (state.view === 'profile' && button.dataset.view === 'profiles');
    button.classList.toggle('active', current);
  });

  const views = { profiles: profilesView, bulk: bulkView, users: usersView, profile: profileView };
  const { html, actions } = views[state.view]();
  $('#view').innerHTML = html;
  $('#top-actions').innerHTML = actions || '';

  bind();
}

/**
 * Whether a form in the view holds unsaved input, or the focus: a poll that
 * re-rendered now would throw it away.
 */
function formDirty() {
  const view = $('#view');
  const active = document.activeElement;
  if (active && view.contains(active) && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName)) return true;

  const changed = (element) => {
    if (element.type === 'checkbox') return element.checked !== element.defaultChecked;

    if (element.tagName === 'SELECT') {
      const defaultIndex = [...element.options].findIndex((option) => option.defaultSelected);
      return element.selectedIndex !== defaultIndex && element.selectedIndex !== 0;
    }

    return element.value !== element.defaultValue;
  };

  return [...view.querySelectorAll('input, select, textarea')].some(changed);
}

function phasePill(profile) {
  const suspended = profile.suspended ? ' (suspended)' : '';
  return `<span class="phase ${esc(profile.phase)}"></span>${esc(profile.phase)}${suspended}`;
}

function accessBadge(flag) {
  if (flag === 'rw') return '<span class="badge rw">rw</span>';
  if (flag === 'ssh') return '<span class="badge ssh">ssh</span>';
  return '<span class="badge ro">read-only</span>';
}

/**
 * A profile's languages for the list of profiles: a badge for each (its
 * tools are left to its own page), or "all" or "none".
 */
function languageBadges(keys) {
  const languages = state.catalogue.filter((lang) => lang.kind === 'language');
  const chosen = languages.filter((lang) => (keys || []).includes(lang.key));

  if (!chosen.length) {
    return '<span class="muted small">none</span>';
  }
  if (chosen.length === languages.length) {
    return '<span class="muted small">all</span>';
  }

  const badges = chosen.map((lang) => `<span class="badge">${esc(lang.name)}</span>`).join('');
  return `<div class="language-badges">${badges}</div>`;
}

/** The list of profiles, with the search box and the button that makes a new one. */
function profilesView() {
  const query = state.query.toLowerCase();
  const matching = state.profiles.filter((profile) => !query || profile.name.includes(query) || (profile.image || '').includes(query));

  const rows = matching
    .map(
      (profile) => `<tr class="clickable" data-open="${esc(profile.name)}">
    <td class="mono"><b>${esc(profile.name)}</b></td><td>${phasePill(profile)}<div class="muted small">${esc(profile.message)}</div></td>
    <td>${languageBadges(profile.languages)}</td>
    <td class="mono small nowrap">${esc(profile.image)}</td><td class="nowrap">${profile.entries} <span class="muted small">(${profile.rw} rw · ${profile.ssh} ssh)</span></td>
    <td class="muted small nowrap">${esc((profile.createdAt || '').slice(0, 10))}</td></tr>`,
    )
    .join('');

  const ready = state.profiles.filter((profile) => profile.phase === 'Ready').length;
  const empty =
    '<tr><td colspan="6" class="empty">No profiles yet. Create one: each is a complete sandbox (VS Code, Docker, proxy, DNS) built around the languages and tools you choose, with its own allowlist.</td></tr>';

  const toolbar =
    `<div class="toolbar"><input type="text" id="q" placeholder="Search profiles…" value="${esc(state.query)}">` +
    `<span class="spacer"></span><span class="muted small">${state.profiles.length} profiles · ${ready} ready</span>` +
    `<button class="btn small" data-act="refresh">Refresh</button></div>`;

  return {
    actions: `<button class="btn primary" data-act="new-profile">＋ New profile</button>`,
    html: `
      ${toolbar}
      <div class="card" style="padding:0"><table><thead><tr><th>Name</th><th>Status</th><th>Languages</th><th>Image</th><th>Allowlist</th><th>Created</th></tr></thead>
      <tbody>${rows || empty}</tbody></table></div>`,
  };
}

/** Mono chips, one per value, each with its value as the tooltip (a long one is cut short); *none* when there are no values. */
function chipList(values, none) {
  if (!values.length) {
    return `<span class="muted small">${esc(none)}</span>`;
  }

  const chips = values.map((value) => `<span class="value-chip mono" title="${esc(value)}">${esc(value)}</span>`).join('');
  return `<div class="value-chips">${chips}</div>`;
}

/** One language's or tool's card: its switch, its toolchain, what it opens and what it brings. */
function languageCard(lang) {
  const checked = lang.enabled ? 'checked' : '';

  return `<div class="card language-card ${lang.enabled ? 'on' : ''}">
      <div class="language-head"><div><b>${esc(lang.name)}</b><div class="muted small">${esc(lang.desc)}</div></div>
        <label class="switch" title="${esc(lang.enabled ? 'Switch off' : 'Switch on')}"><input type="checkbox" data-lang="${esc(lang.key)}" ${checked}><span></span></label></div>
      <div class="language-part"><div class="language-label">Opens</div>${chipList(lang.domains, 'no allowlist entries')}</div>
      <div class="language-part"><div class="language-label">Brings</div>${chipList(lang.extensions, 'no extensions')}</div></div>`;
}

/**
 * The languages / tools tab: what the profile is built around, languages
 * first and then the tools, each switchable; then its own Ubuntu packages.
 */
function languagesTab(profile) {
  const languages = profile.languages.filter((lang) => lang.kind === 'language');
  const tools = profile.languages.filter((lang) => lang.kind === 'tool');

  /** "3 of 9 on", for one group. */
  const count = (group) => `${group.filter((lang) => lang.enabled).length} of ${group.length} on`;

  const note =
    '<div class="callout info">On Kubernetes the image (<b>Settings</b>) decides which toolchains a VS Code container has; the default image has them all. ' +
    'A language or tool opens its allowlist entries at once (the proxy reloads), and the running VS Code container installs or removes its VS Code extensions in the background within about a minute, without a restart.</div>';

  return `${note}
      <div class="group-head"><h2>Languages</h2><span class="muted small">${count(languages)}</span></div>
      <div class="language-grid">${languages.map(languageCard).join('')}</div>
      <div class="group-head"><h2>Tools</h2><span class="muted small">${count(tools)}</span></div>
      <div class="language-grid">${tools.map(languageCard).join('')}</div>
      ${packagesCard(profile)}`;
}

/**
 * The Ubuntu packages card: the profile's own packages, each with its ✕,
 * and the box that adds more (several at once, separated by spaces). The
 * image is shared on Kubernetes, so the VS Code container installs them as
 * it starts: a change restarts it, and every start takes longer for them.
 */
function packagesCard(profile) {
  const chips = profile.packages
    .map(
      (name) =>
        `<span class="package-chip">${esc(name)}` +
        `<button class="btn ghost small icon" data-pkg-remove="${esc(name)}" title="Remove; the VS Code container restarts without it">✕</button></span>`,
    )
    .join('');
  const none = '<span class="muted small">None yet.</span>';

  return `<div class="card" id="packages-card" style="margin-top:14px"><h2>Ubuntu packages</h2>
      <p class="muted small" style="margin-top:0">This profile's own, from Ubuntu 26.04's archive. The image is shared on Kubernetes, so the VS Code container installs them through the proxy as it starts: a change restarts it, and every start takes longer for them. When one can't be installed, none are, and the container's log says why.</p>
      <div class="value-chips">${chips || none}</div>
      <div class="form-row">
        <div class="field grow"><label>Package</label><input type="text" class="mono" id="pk-name" placeholder="its name in Ubuntu's archive: postgresql-client"></div>
        <button class="btn primary" data-act="pkg-add">Add</button></div>
      <div class="form-error" id="pk-error"></div></div>`;
}

/**
 * How far the running VS Code container has got with the profile's
 * extensions (the profile's extensionStatus, read from the container's log):
 * installing, or done, with what it refused and what it will try again. The
 * element carries an id so that the refresh can replace it on its own while
 * a form on the tab holds input.
 */
function extensionStatus(status) {
  if (!status) {
    return '<div class="callout info" id="ext-status">No word from the VS Code container about its extensions yet: it isn\'t running, or has only just started.</div>';
  }

  let progress = `${status.installed} of ${status.total} installed`;
  if (status.state === 'installing') {
    progress = `Installing in the background: ${status.installed} of ${status.total} in place`;
  } else if (status.installed === status.total) {
    progress = `All ${status.total} installed`;
  }

  const parts = [`<b>${esc(progress)}</b>`];
  if (status.refused) {
    parts.push(`${esc(status.refused)} refused: a publisher without a verified domain`);
  }
  if (status.failed) {
    parts.push(`${esc(status.failed)} to try again: they couldn't be installed or checked`);
  }

  // Blue while installing, green once everything is in place, and the
  // callout's own amber when something was refused or is still missing.
  let kind = 'info';
  if (status.refused) {
    kind = '';
  } else if (status.state === 'done') {
    kind = status.installed === status.total ? 'ok' : '';
  }

  return `<div class="callout ${kind}" id="ext-status">${parts.join(' · ')}</div>`;
}

/**
 * The extensions tab: how far the VS Code container has got with them; what
 * every VS Code container has and what each language that is on brings, as
 * chips; then the profile's own, which are added and removed here.
 */
function extensionsTab(profile) {
  const extensions = profile.extensions;
  const bringing = profile.languages.filter((lang) => lang.enabled && lang.extensions.length);
  const offLanguages = profile.languages.filter((lang) => !lang.enabled && lang.extensions.length);

  /** One source's line: its name, then its extensions. */
  const source = (name, ids) => `<div class="source-row"><div class="source-name">${esc(name)}</div>${chipList(ids, '')}</div>`;

  const core = extensions.filter((extension) => extension.source === 'core').map((extension) => extension.id);
  let given = source('Every VS Code container', core);
  for (const lang of bringing) {
    given += source(lang.name, lang.extensions);
  }

  const off = offLanguages.length
    ? `<p class="muted small" style="margin:10px 0 0">Not installed, their languages being off: ${esc(offLanguages.map((lang) => lang.name).join(', '))}. Switching one on under <b>Languages / Tools</b> brings its extensions.</p>`
    : '';

  const custom = extensions.filter((extension) => extension.source === 'custom');
  const own = custom.map((extension) => ownExtensionRow(extension, extensions)).join('');
  const none = '<tr><td colspan="3" class="muted small">None yet.</td></tr>';

  return `${extensionStatus(profile.extensionStatus)}
      <div class="card" style="margin-bottom:14px"><h2>Included</h2>${given}${off}</div>
      <div class="card"><h2>Your extensions</h2><div class="form-row">
        <div class="field grow"><label>Extension id</label><input type="text" class="mono" id="ex-id" placeholder="publisher.name, as the Marketplace shows it (eamodio.gitlens)"></div>
        <button class="btn primary" data-act="ext-add">Add</button></div>
        <div class="form-error" id="ex-error"></div>
        <table class="own-extensions"><tbody>${own || none}</tbody></table>
        <p class="muted small" style="margin:10px 0 0">Only extensions whose publisher has a domain the Marketplace has verified are installed; any other is refused, and the VS Code container's log says so. A change reaches the running VS Code container within about a minute: it installs or removes the extension in the background, without a restart.</p></div>`;
}

/**
 * One of the profile's own extensions, with its remove button. One the
 * vscode gets anyway (every VS Code container's, or a language's that is on) is marked
 * as such.
 */
function ownExtensionRow(extension, extensions) {
  const id = esc(extension.id);

  const provider = extensions.find((other) => other.id === extension.id && other.source !== 'custom' && other.enabled);
  let note = '';
  if (provider) {
    const from = provider.source === 'core' ? 'every VS Code container' : languageName(provider.source);
    note = `<span class="badge rw">already comes with ${esc(from)}</span>`;
  }

  return (
    `<tr><td class="mono">${id}</td><td>${note}</td>` +
    `<td class="actions"><button class="btn ghost small icon" data-ext-remove="${id}" title="Remove">✕</button></td></tr>`
  );
}

/** The heading of an allowlist section: where its entries come from. */
function allowlistSectionTitle(entry) {
  if (entry.provided && entry.language) {
    return `${languageName(entry.language)} · comes with the ${languageKind(entry.language)}`;
  }
  if (entry.provided) {
    return `${entry.section} · every profile`;
  }

  return `${entry.section || 'Custom'} · this profile's own`;
}

/**
 * The allowlist tab: the add form, and every entry by section: the
 * profile's own, which one just added joins, then those HardCode provides
 * (every profile's, then the languages' and tools').
 *
 * A provided entry carries a badge where the delete button would be: it
 * comes with every profile, or with its language (Languages / Tools). It
 * can still be switched off, and stays off through updates.
 */
function allowlistTab(profile) {
  let rows = '';
  let lastSection = null;

  for (const entry of profile.allowlist) {
    // A section of the profile's own may share a block's name: where an
    // entry comes from tells them apart.
    const section = `${entry.source}/${entry.section}`;
    if (section !== lastSection) {
      rows += `<tr class="section-row"><td colspan="5">${esc(allowlistSectionTitle(entry))}</td></tr>`;
      lastSection = section;
    }

    // The flag travels with the entry: an SSH entry and the HTTPS entry for
    // the same host are different rules.
    const name = esc(entry.entry);
    const flag = esc(entry.flag);
    const checked = entry.enabled ? 'checked' : '';

    // Where the delete button would be, a provided entry has a badge
    // instead, saying where it comes from.
    let remove = `<button class="btn ghost small icon" data-remove="${name}" data-flag="${flag}" title="Delete">✕</button>`;
    if (entry.provided && entry.language) {
      const language = languageName(entry.language);
      const why = `Comes with ${language}: switch it off here, or the language off under Languages / Tools`;
      remove = `<span class="badge language" title="${esc(why)}">${esc(language)}</span>`;
    } else if (entry.provided) {
      const why = 'Comes with every profile: switch it off here to stop allowing it';
      remove = `<span class="badge language" title="${esc(why)}">every profile</span>`;
    }

    rows +=
      `<tr class="${entry.enabled ? '' : 'disabled'}">` +
      `<td style="width:48px"><label class="switch"><input type="checkbox" data-toggle="${name}" data-flag="${flag}" ${checked}><span></span></label></td>` +
      `<td class="entry mono">${name}</td>` +
      `<td class="nowrap"><span class="badge">${esc(entry.kind)}</span> ${accessBadge(entry.flag)}</td>` +
      `<td class="muted small">${esc(entry.comment)}</td>` +
      `<td class="actions">${remove}</td></tr>`;
  }

  return `
      <div class="card" style="margin-bottom:14px"><div class="form-row">
        <div class="field grow"><label>Entry</label><input type="text" class="mono" id="al-entry" placeholder="example.com · .example.com · github.com/team · host:22 (ssh)" value="${esc(state.prefill)}"></div>
        <div class="field"><label>Access</label><select id="al-flag"><option value="">read-only (GET, clone/pull)</option><option value="rw">read-write (uploads, push)</option><option value="ssh" ${state.prefillFlag === 'ssh' ? 'selected' : ''}>ssh tunnel</option></select></div>
        <button class="btn primary" data-act="al-add">Add</button></div>
        <div class="form-error" id="al-error"></div>
        <p class="muted small" style="margin:0">The entries HardCode provides (every profile's, and those of the languages and tools that are on) can be switched off but not deleted: one switched off stays off through updates, and one an update adds arrives switched on. Changes are applied to the running proxy within seconds (squid and CoreDNS restart with the new rules). To change many profiles at once use <b>All allowlists</b>. <button class="btn ghost small" data-act="al-raw">Edit as text…</button></p></div>
      ${lanCard(profile)}
      <div class="card" style="padding:0"><table><thead><tr><th></th><th>Entry</th><th>Type / access</th><th>Note</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

/**
 * A pin as the last Check found it: its state and what its name resolved
 * to, when that Check was for this profile and the pin still has the
 * address it had then; "unchecked" otherwise, as the profile gives it.
 */
function checkedPin(profileName, pin) {
  const checked = state.lanChecks.profile === profileName ? state.lanChecks.pins[pin.name] : null;
  if (!checked || checked.address !== pin.address) {
    return pin;
  }

  return { ...pin, resolves: checked.resolves, state: checked.state };
}

/** A pin's state on the On your network card, in words. */
function lanStatus(pin) {
  if (pin.problem) {
    return `invalid: ${pin.problem}`;
  }
  if (pin.ignored) {
    return `ignored: ${pin.ignored}`;
  }
  if (pin.state === 'unchecked') {
    return 'not checked';
  }
  if (pin.state === 'moved') {
    return `now resolves to ${pin.resolves.join(', ')}`;
  }
  if (pin.state === 'unresolved') {
    return "ok (doesn't resolve from the admin)";
  }
  return 'ok';
}

/**
 * Why the operator takes none of this profile's pins, when it doesn't: the
 * chart doesn't switch them on, or doesn't list the cluster's own ranges.
 */
function lanOff(view) {
  if (!view.off) {
    return '';
  }
  if (!view.enabled) {
    return '<div class="callout">This cluster doesn\'t let names reach its network: the operator ignores these until the chart sets <code>profileDefaults.lan.enabled</code>.</div>';
  }
  return `<div class="callout">The operator ignores these: ${esc(view.off)}, so that no name can reach the cluster's own API server, pods or nodes.</div>`;
}

/**
 * The On your network card of the allowlist tab: the names this profile
 * lets reach an address on the network, each with its state, and a form to
 * add one. The admin's pod usually can't see that network, so the address
 * is typed, never looked up; the names are looked up only on Check. When
 * the operator takes no pins, it says why.
 */
function lanCard(profile) {
  const view = profile.lan;

  const rows = view.pins
    .map((listed) => {
      const pin = checkedPin(profile.name, listed);

      // A name that moved keeps its address: the move is filled into the
      // form, for someone to look at and save, never followed.
      const move =
        pin.state === 'moved' && !pin.problem
          ? `<button class="btn small" data-lan-move="${esc(pin.name)}" data-address="${esc(pin.resolves[0])}">Move…</button> `
          : '';

      return (
        `<tr><td class="entry mono">${esc(pin.name)}</td><td class="mono">${esc(pin.address)}</td>` +
        `<td class="muted small">${esc(lanStatus(pin))}</td>` +
        `<td class="actions">${move}<button class="btn ghost small icon" data-lan-remove="${esc(pin.name)}" title="Remove">✕</button></td></tr>`
      );
    })
    .join('');

  const off = lanOff(view);
  const empty = '<tr><td colspan="4" class="empty">No name of this profile reaches your network.</td></tr>';
  const check = view.pins.length
    ? '<button class="btn small" data-act="lan-check" title="Look the names up from the admin, as they resolve now">Check</button>'
    : '';

  return `<div class="card" id="lan-card" style="margin-bottom:14px"><h2>On your network</h2>${off}
      <p class="muted small">The proxy refuses names that point inside your own network. A name listed here may reach the address shown, and nothing else on your network.</p>
      <div class="form-row">
        <div class="field grow"><label>Name</label><input type="text" class="mono" id="lan-name" placeholder="gitlab.example.com" value="${esc(state.lanPrefill.name)}"></div>
        <div class="field"><label>Address</label><input type="text" class="mono" id="lan-address" placeholder="192.168.71.200" value="${esc(state.lanPrefill.address)}"></div>
        <button class="btn" data-act="lan-add">Let it reach that address</button></div>
      <div class="form-error" id="lan-error"></div>
      <table><thead><tr><th>Name</th><th>Address</th><th>State</th><th class="actions">${check}</th></tr></thead><tbody>${rows || empty}</tbody></table></div>`;
}

// Why the proxy refused, when it said, as Blocked traffic shows it.
const REFUSED_BECAUSE = { 'internal-address': 'resolves to an internal address' };

/**
 * A Blocked traffic row's buttons. An SSH tunnel's "Allow host" fills in
 * its host and port, with ssh; a name refused for pointing inside your
 * network is offered On your network instead, since an entry wouldn't let
 * it through.
 */
function blockedActions(target) {
  if (target.reason === 'internal-address') {
    return `<button class="btn small" data-allow-lan="${esc(target.host)}">Allow on your network</button>`;
  }

  // "Allow URL" offers the URL's directory, without its scheme.
  const directory = target.url ? target.url.replace(/^https?:\/\//, '').replace(/\/[^/]*$/, '') : '';
  const allowUrl = target.url ? `<button class="btn small" data-allow="${esc(directory)}">Allow URL</button>` : '';

  let host = target.host;
  let flag = '';
  if (target.port) {
    host = target.port === 22 ? target.host : `${target.host}:${target.port}`;
    flag = 'ssh';
  }

  return `<button class="btn small" data-allow="${esc(host)}" data-flag="${esc(flag)}">Allow host</button>${allowUrl}`;
}

/** The blocked-traffic tab: what the proxy and DNS filter refused, and why when it said. */
function blockedTab() {
  const rows = state.blocked
    .map((target) => {
      const what = target.kind === 'dns' ? 'DNS ' + esc(target.method) : esc(target.method);
      const why = REFUSED_BECAUSE[target.reason] ? `<div class="muted small">${esc(REFUSED_BECAUSE[target.reason])}</div>` : '';

      return (
        `<tr><td><span class="badge ${target.kind === 'dns' ? 'dns' : ''}">${what}</span></td>` +
        `<td class="mono">${esc(target.url || target.target)}${why}</td><td>${esc(target.count)}</td>` +
        `<td class="actions">${blockedActions(target)}</td></tr>`
      );
    })
    .join('');

  const periods = ['10m', '30m', '2h', '24h']
    .map((period) => `<option ${state.since === period ? 'selected' : ''}>${period}</option>`)
    .join('');
  const empty = '<tr><td colspan="4" class="empty">Nothing blocked in this period (or the pods aren\'t running yet).</td></tr>';

  const toolbar =
    `<div class="toolbar"><span class="muted">Refused in the last</span><select id="since">${periods}</select>` +
    `<button class="btn small" data-act="refresh-blocked">Refresh</button></div>`;

  return `${toolbar}
      <div class="callout info">"Allow host" and "Allow URL" pre-fill the allowlist form; nothing is allowed until you add it there.</div>
      <div class="card" style="padding:0"><table><thead><tr><th>What</th><th>Target</th><th>Count</th><th></th></tr></thead><tbody>${rows || empty}</tbody></table></div>`;
}

/** The access tab: how to reach the profile's VS Code. */
function accessTab(profile) {
  const access = profile.access;
  const status = profile.status;

  // The token is a credential: shown only when asked for, never in a
  // link's text. The links still open the real URL.
  const shown = state.showToken ? access.url : maskedUrl(access.url);
  const how = access.host
    ? `<p>Published at <a href="${esc(access.url)}" target="_blank" rel="noopener">${esc('https://' + access.host)}</a> (Ingress). Put TLS and your own sign-in in front of it; the token below is the only protection otherwise.</p>`
    : `<p>No <code>vscodeHost</code> set, so reach it with a port-forward from a machine with cluster access:</p><pre class="mono small">${esc(access.portForward)}</pre><p>then open <a href="${esc(access.url)}" target="_blank" rel="noopener">${esc(maskedUrl(access.url))}</a></p>`;

  return `<div class="card accent"><h2>VS Code</h2>
      ${how}
      <div class="url-box"><input type="text" class="mono small" readonly value="${esc(shown)}" id="vs-url"><button class="btn small" data-act="toggle-token">${state.showToken ? 'Hide' : 'Show'}</button><button class="btn small" data-act="copy-url">Copy</button></div>
      <div style="margin-top:10px"><button class="btn small" data-act="rotate-token" title="A new token: every copy of this one stops working, and VS Code restarts">New token…</button></div>
      <p class="muted small" style="margin-top:10px">Token secret: <code>${esc(status.tokenSecret || '')}</code> · proxy: <code>${esc(status.proxyURL || '')}</code> · DNS: <code>${esc(status.dnsClusterIP || '')}</code></p></div>`;
}

/**
 * A URL with its token hidden: the token is a credential, and a screen is
 * seen by more than its owner (a screenshot, a shared screen).
 */
function maskedUrl(url) {
  return String(url || '').replace(/([?&]tkn=)[^&#]+/, '$1••••••••');
}

/**
 * The settings tab. Empty fields show the inherited value as a placeholder;
 * left empty, the profile keeps inheriting it.
 */
function settingsTab(profile) {
  const spec = profile.spec;
  const effective = spec.effective;

  return `<div class="card"><h2>Settings</h2><div class="kv">
      <div class="k">Image</div><div><input type="text" class="mono" id="st-image" value="${esc(spec.image || '')}" placeholder="${esc(effective.image)}" style="width:100%"></div>
      <div class="k">VS Code host (Ingress)</div><div><input type="text" class="mono" id="st-host" value="${esc(spec.vscodeHost || '')}" placeholder="none: use port-forward" style="width:100%"></div>
      <div class="k">Home volume</div><div><input type="text" class="mono" id="st-home" value="${esc((spec.home && spec.home.size) || '')}" placeholder="${esc(effective.home.size)}" style="width:120px"> <span class="muted small">(an existing volume is grown when its StorageClass allows; it can't shrink)</span></div>
      <div class="k">Docker sidecar</div><div><label class="check"><input type="checkbox" id="st-docker" ${effective.docker.enabled ? 'checked' : ''}> enabled (privileged container)</label></div>
      <div class="k">Suspended</div><div><label class="check"><input type="checkbox" id="st-suspended" ${effective.suspended ? 'checked' : ''}> scale the VS Code container to 0 (keeps its volumes)</label></div>
      </div><div style="margin-top:14px"><button class="btn primary" data-act="save-settings" data-version="${esc(profile.version)}">Save</button></div></div>
      <div class="card" style="margin-top:14px"><h2>Status</h2><pre class="mono small" style="white-space:pre-wrap">${esc(JSON.stringify(profile.status, null, 2))}</pre></div>
      <div class="card" style="margin-top:14px;border-color:rgba(248,113,113,.35)"><h2>Delete profile</h2><p class="muted small">Removes the pods, services, policies, secrets and every volume (home directory included).</p><button class="btn danger" data-act="delete-profile">Delete ${esc(profile.name)}…</button></div>`;
}

/** One profile: its tabs, and in the header its phase and what can be done with it. */
function profileView() {
  const profile = state.profile;
  if (!profile) return { html: '<div class="empty">Loading…</div>' };

  const tabs = Object.keys(TAB_TITLES)
    .map((tab) => `<button class="tab ${state.tab === tab ? 'active' : ''}" data-tab="${tab}">${TAB_TITLES[tab]}</button>`)
    .join('');

  const bodies = {
    languages: () => languagesTab(profile),
    extensions: () => extensionsTab(profile),
    allowlist: () => allowlistTab(profile),
    blocked: blockedTab,
    access: () => accessTab(profile),
    settings: () => settingsTab(profile),
  };
  const body = bodies[state.tab]();

  // Amber while on its way somewhere; a profile being deleted is going away,
  // which the plain pill says better than a warning.
  let phaseClass = 'warn';
  if (profile.phase === 'Ready') {
    phaseClass = 'ok';
  }
  if (profile.phase === 'Error') {
    phaseClass = 'bad';
  }
  if (profile.phase === 'Deleting') {
    phaseClass = '';
  }

  const openVscode =
    profile.access && profile.access.host
      ? `<a class="btn primary" href="${esc(profile.access.url)}" target="_blank" rel="noopener">Open VS Code ↗</a>`
      : '';

  return {
    actions: `<span class="pill ${phaseClass}">${esc(profile.phase)}${profile.suspended ? ' · suspended' : ''}</span>
      <button class="btn" data-act="toggle-suspend">${profile.suspended ? '▶ Resume' : '■ Suspend'}</button>
      ${openVscode}
      <button class="btn ghost" data-act="back">← Profiles</button>`,
    html: `<div class="tabs">${tabs}</div>${body}`,
  };
}

/**
 * Every profile's allowlist at once: one entry, and what to do with it. The
 * entry and access last used are drawn back in, so the form isn't left
 * holding unsaved input that stops the refresh from redrawing it.
 */
function bulkView() {
  const buttons =
    '<button class="btn primary" data-bulk="add">Add to all</button>' +
    '<button class="btn" data-bulk="enable">Enable in all</button>' +
    '<button class="btn" data-bulk="disable">Disable in all</button>' +
    '<button class="btn danger" data-bulk="remove">Remove from all</button>';

  const accesses = [
    ['', 'read-only'],
    ['rw', 'read-write'],
    ['ssh', 'ssh tunnel'],
  ];
  const options = accesses
    .map(([flag, label]) => `<option value="${esc(flag)}" ${state.bulkFlag === flag ? 'selected' : ''}>${esc(label)}</option>`)
    .join('');

  return {
    html: `<div class="callout">Changes here apply to <b>every profile</b> (${state.profiles.length}) at once: add an entry, remove it, or enable/disable it. Each profile's proxy reloads on its own.</div>
      <div class="card"><div class="form-row">
        <div class="field grow"><label>Entry</label><input type="text" class="mono" id="bk-entry" placeholder="github.com/ourcompany" value="${esc(state.bulkEntry)}"></div>
        <div class="field"><label>Access</label><select id="bk-flag">${options}</select></div>
        ${buttons}</div>
        <div class="form-error" id="bk-error"></div>
        <p class="muted small" style="margin:0">"ssh tunnel" also picks the ssh entry to remove, enable or disable: an ssh entry and the HTTPS entry for the same host are separate rules. An entry HardCode provides can be disabled everywhere, not removed.</p>
        <pre class="mono small" id="bk-result">${esc(state.bulkResult)}</pre></div>`,
  };
}

function usersView() {
  const rows = state.users
    .map((user) => {
      const you = user.email === state.me.email ? ' <span class="badge">you</span>' : '';
      const twoFactor = user.totpEnrolled ? '<span class="badge ro">enrolled</span>' : '<span class="badge rw">pending</span>';

      return (
        `<tr><td>${esc(user.email)}${you}</td><td>${twoFactor}</td>` +
        `<td class="muted small">${esc((user.createdAt || '').slice(0, 10))}</td>` +
        `<td class="actions"><button class="btn small" data-reset-totp="${esc(user.email)}">Reset 2FA</button> ` +
        `<button class="btn ghost small icon" data-del-user="${esc(user.email)}" title="Remove">✕</button></td></tr>`
      );
    })
    .join('');

  const form =
    '<div class="card" style="margin-bottom:14px"><div class="form-row">' +
    '<div class="field grow"><label>Email</label><input type="email" id="us-email"></div>' +
    '<div class="field grow"><label>Temporary password (12+ characters)</label><input type="text" id="us-password" class="mono"></div>' +
    '<button class="btn primary" data-act="add-user">Add admin</button></div><div class="form-error" id="us-error"></div>' +
    '<p class="muted small" style="margin:0">Every admin can do everything here. New admins enrol their authenticator at first sign-in; accounts live in the <code>hardcode-admin-users</code> Secret.</p></div>';

  return {
    html: `${form}
      <div class="card" style="padding:0"><table><thead><tr><th>Email</th><th>2FA</th><th>Added</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`,
  };
}

// ---------------------------------------------------------------------------
// Dialogs
// ---------------------------------------------------------------------------

/** Show a dialog; *onOpen* wires up its controls. Anything marked data-close closes it. */
function modal(html, onOpen) {
  const card = $('#modal-card');
  card.innerHTML = html;
  $('#modal').classList.remove('hidden');

  card.querySelectorAll('[data-close]').forEach((button) => {
    button.onclick = closeModal;
  });

  if (onOpen) onOpen(card);
}

function closeModal() {
  $('#modal').classList.add('hidden');
}

/**
 * The languages a new profile starts with: a copy's are its source's; one
 * from the template has the tools and no languages, which are chosen next.
 */
function startingLanguages(source) {
  if (source) {
    const profile = state.profiles.find((candidate) => candidate.name === source);
    return profile ? profile.languages : [];
  }

  return state.catalogue.filter((lang) => lang.kind === 'tool').map((lang) => lang.key);
}

/** The picker's checkboxes for one kind of the catalogue: "language" or "tool". */
function languagePicker(kind) {
  const boxes = state.catalogue
    .filter((lang) => lang.kind === kind)
    .map((lang) => `<label class="check" title="${esc(lang.desc)}"><input type="checkbox" data-pick="${esc(lang.key)}">${esc(lang.name)}</label>`)
    .join('');

  return `<div class="language-picker">${boxes}</div>`;
}

/**
 * The new-profile dialog: a name, where it starts from, the languages and
 * tools it is built around, and optionally an image and a host name.
 */
function newProfileModal() {
  const sources = state.profiles.map((profile) => `<option value="${esc(profile.name)}">copy of ${esc(profile.name)}</option>`).join('');

  const html = `<h2>New profile</h2><p class="muted small">A complete sandbox: VS Code, rootless Docker, proxy and DNS filter, built around the languages and tools you choose, with its own allowlist and volumes.</p>
    <div class="field"><label>Name</label><input type="text" class="mono" id="np-name" placeholder="team-a"></div>
    <div class="field"><label>Allowlist and settings from</label><select id="np-from"><option value="">the template</option>${sources}</select></div>
    <div class="field"><label>Languages</label>${languagePicker('language')}</div>
    <div class="field"><label>Tools</label>${languagePicker('tool')}</div>
    <p class="muted small" style="margin:0">A language or tool opens its allowlist entries and brings its VS Code extensions, installed in the background; the image decides which toolchains there are.</p>
    <div class="field"><label>VS Code image (optional)</label><input type="text" class="mono" id="np-image" placeholder="registry.example.com/hardcode/vscode:1.0"></div>
    <div class="field"><label>VS Code host for an Ingress (optional)</label><input type="text" class="mono" id="np-host" placeholder="team-a.code.example.com"></div>
    <div class="form-error" id="np-error"></div>
    <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="np-create">Create</button></div>`;

  modal(html, (card) => {
    const field = (id) => card.querySelector(id);
    const boxes = [...card.querySelectorAll('[data-pick]')];
    const picked = () => boxes.filter((box) => box.checked).map((box) => box.dataset.pick);

    // The picker starts from where the profile starts from.
    let starting = [];
    const pickStarting = () => {
      starting = startingLanguages(field('#np-from').value);
      boxes.forEach((box) => {
        box.checked = starting.includes(box.dataset.pick);
      });
    };
    field('#np-from').onchange = pickStarting;
    pickStarting();

    field('#np-create').onclick = async () => {
      const source = field('#np-from').value;
      const body = {
        name: field('#np-name').value.trim(),
        from: source,
        image: field('#np-image').value.trim(),
        vscodeHost: field('#np-host').value.trim(),
      };

      // A copy left with its source's languages keeps its source's allowlist
      // as it is; other languages, or the template, bring the blocks in step.
      // Without the catalogue there is nothing to choose from: the profile
      // gets the default. (Both lists are in catalogue order.)
      const languages = picked();
      const changed = languages.join() !== starting.join();
      if (state.catalogue.length && (!source || changed)) {
        body.languages = languages;
      }

      let created;
      try {
        created = await api('POST', '/api/profiles', body);
      } catch (error) {
        field('#np-error').textContent = error.message;
        return;
      }

      // The name as the API made it ("Team-A" becomes "team-a"), not as typed.
      closeModal();
      toast(`Profile ${created.name} created; the operator is bringing it up`, 'ok');
      await loadProfiles();
      openProfile(created.name, 'languages');
    };

    field('#np-name').focus();
  });
}

/** The profile's own allowlist as text, in a modal, saved whole (on the version it was opened at). */
function rawAllowlistModal() {
  const text = state.profile.spec.effective.allowlist;

  // The version the text was taken at. The poll keeps state.profile current
  // while the editor is open, so it can't be read at save time.
  const openedAt = state.profile.version;

  const html = `<h2>Allowlist of ${esc(state.profile.name)}</h2><p class="muted small">The profile's own entries, and an <code>off &lt;entry&gt;</code> line for each provided entry it switches off. The provided entries aren't here: they come with every profile and with its languages and tools, and the Allowlist tab lists them.</p><textarea id="raw-text">${esc(text)}</textarea><div class="form-error" id="raw-error"></div>
    <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="raw-save">Save</button></div>`;

  modal(html, (card) => {
    card.querySelector('#raw-save').onclick = async () => {
      try {
        await api('PATCH', profilePath(), { allowlist: card.querySelector('#raw-text').value, version: openedAt });
      } catch (error) {
        card.querySelector('#raw-error').textContent = error.message;
        return;
      }

      closeModal();
      await openProfile(state.profile.name);
      toast('Allowlist saved', 'ok');
    };
  });
}

// ---------------------------------------------------------------------------
// Loading
// ---------------------------------------------------------------------------

/**
 * The list of profiles, fetched again, and the view redrawn unless a form
 * in it holds input. *poll* marks the background refresh's request.
 */
async function loadProfiles(poll = false) {
  state.profiles = await call('GET', '/api/profiles', undefined, { poll });
  if ((state.view === 'profiles' || state.view === 'bulk') && !formDirty()) {
    render();
  }
}

/**
 * Show one profile, at *tab* when one is given (opened from the list, it is
 * its languages); without, at the tab it was showing, as after an edit. It
 * is fetched before the view changes: a profile that can't be opened (the
 * toast says why) leaves the page where it was, with the list still
 * refreshing.
 */
async function openProfile(name, tab) {
  const profile = await call('GET', `/api/profiles/${name}`);

  state.view = 'profile';
  state.profile = profile;
  if (tab) {
    state.tab = tab;
  }
  render();

  if (state.tab === 'blocked') actions['refresh-blocked']();
}

/**
 * Switch one of the open profile's languages or tools on or off. The list
 * sent is made from the profile as it is now, not as the page last drew it,
 * so a switch another admin made meanwhile isn't undone. The operator gives
 * the proxy the language's allowlist entries or takes them away; the
 * running VS Code container follows with the extensions by itself.
 */
async function setLanguage(key, enabled) {
  const current = await call('GET', profilePath());

  const others = current.languages.filter((lang) => lang.enabled && lang.key !== key).map((lang) => lang.key);
  const languages = enabled ? [...others, key] : others;

  await call('PATCH', profilePath(), { languages });
  await openProfile(state.profile.name);

  const lang = state.catalogue.find((entry) => entry.key === key);
  let what = enabled ? 'its allowlist entries are open' : 'its allowlist entries are closed';
  if (lang && lang.extensions.length) {
    const how = enabled ? 'installed' : 'removed';
    what += `; its VS Code extensions are ${how} in the background within about a minute`;
  }

  toast(`${languageName(key)} ${enabled ? 'on' : 'off'}: ${what}`, 'ok');
}

/**
 * One edit of the open profile's own extensions; the answer says what
 * happened, and a change is on its way to the running VS Code container.
 */
async function editExtensions(op, id) {
  const answer = await api('POST', profilePath('/extensions'), { op, id });
  await openProfile(state.profile.name);

  let message = answer.message;
  if (answer.changed) {
    const how = op === 'add' ? 'installs' : 'removes';
    message += `; the VS Code container ${how} it in the background within about a minute`;
  }

  toast(message, answer.changed ? 'ok' : '');
}

/**
 * One edit of the open profile's own Ubuntu packages; the answer says what
 * happened, and that the VS Code container restarts for a change.
 */
async function editPackages(op, names) {
  const answer = await api('POST', profilePath('/packages'), { op, names });
  await openProfile(state.profile.name);

  toast(answer.message, answer.changed ? 'ok' : '');
}

async function reloadUsers() {
  state.users = await call('GET', '/api/users');
  render();
}

// ---------------------------------------------------------------------------
// Actions, by the data-act of the button that starts them
// ---------------------------------------------------------------------------

const actions = {
  'new-profile': newProfileModal,

  // Called with the button, which mustn't land in loadProfiles' *poll*: a
  // click is activity.
  refresh() {
    return loadProfiles();
  },

  back() {
    state.view = 'profiles';
    render();
    loadProfiles();
  },

  'al-raw': rawAllowlistModal,

  async 'ext-add'() {
    const id = $('#ex-id').value.trim();

    try {
      await editExtensions('add', id);
    } catch (error) {
      $('#ex-error').textContent = error.message;
    }
  },

  async 'pkg-add'() {
    const names = $('#pk-name').value.trim().split(/\s+/).filter(Boolean);

    // Checked here first, with the API's rule, so that a name that isn't
    // one is said next to the form at once.
    const bad = names.find((name) => !PACKAGE_NAME.test(name));
    if (!names.length || bad) {
      $('#pk-error').textContent = bad
        ? `'${bad}' is not an Ubuntu package name: lower-case letters, digits, +, - and ., at least two characters`
        : "Type a package's name, as Ubuntu's archive has it";
      return;
    }

    try {
      await editPackages('add', names);
    } catch (error) {
      $('#pk-error').textContent = error.message;
    }
  },

  async 'al-add'() {
    const entry = $('#al-entry').value.trim();
    const flag = $('#al-flag').value;

    try {
      await api('POST', profilePath('/allowlist'), { op: 'add', entry, flag });
    } catch (error) {
      $('#al-error').textContent = error.message;
      return;
    }

    state.prefill = '';
    state.prefillFlag = '';
    await openProfile(state.profile.name);
    toast(`Added ${entry}`, 'ok');
  },

  async 'lan-add'() {
    const name = $('#lan-name').value.trim();
    const address = $('#lan-address').value.trim();

    let answer;
    try {
      answer = await api('POST', profilePath('/lan'), { op: 'add', name, address });
    } catch (error) {
      $('#lan-error').textContent = error.message;
      return;
    }

    state.lanPrefill = { name: '', address: '' };
    await openProfile(state.profile.name);
    toast(answer.message, 'ok');
  },

  // Look the open profile's names on the network up, which the refresh
  // never does: the answer fills in each pin's state, and stays until the
  // next Check or another profile.
  async 'lan-check'() {
    const name = state.profile.name;
    const checked = await call('POST', profilePath('/lan/check'), {});

    const pins = {};
    for (const pin of checked.pins) {
      pins[pin.name] = { address: pin.address, resolves: pin.resolves, state: pin.state };
    }
    state.lanChecks = { profile: name, pins };

    if (state.view === 'profile' && state.profile && state.profile.name === name) {
      render();
    }
  },

  async 'refresh-blocked'() {
    state.blocked = await call('GET', profilePath(`/blocked?since=${state.since}`));
    render();
  },

  'copy-url'() {
    navigator.clipboard.writeText(state.profile.access.url);
    toast('URL copied, token included', 'ok');
  },

  'toggle-token'() {
    state.showToken = !state.showToken;
    render();
  },

  async 'rotate-token'() {
    const name = esc(state.profile.name);
    const html = `<h2>A new VS Code token for ${name}?</h2><p>Every copy of the current token stops working (a screenshot, a shared screen, a browser history), and VS Code restarts with the new one within a minute: open windows need the new URL.</p><div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="rotate-yes">New token</button></div>`;

    modal(html, (card) => {
      card.querySelector('#rotate-yes').onclick = async () => {
        const answer = await call('POST', profilePath('/token'), {});
        closeModal();
        state.showToken = false;
        toast(answer.message, 'ok');
        await openProfile(state.profile.name);
      };
    });
  },

  async 'save-settings'() {
    const body = {
      image: $('#st-image').value.trim(),
      vscodeHost: $('#st-host').value.trim(),
      suspended: $('#st-suspended').checked,
      // The version the form was rendered at, kept in the DOM: the poll moves
      // state.profile on while the form is being filled in.
      version: $('[data-act="save-settings"]').dataset.version,
    };

    // The sidecar's box is drawn from the effective value, which may be the
    // chart's default. Sent only when it was changed, so saving the form
    // doesn't pin the profile to today's default.
    const docker = $('#st-docker');
    if (docker.checked !== docker.defaultChecked) {
      body.docker = { enabled: docker.checked };
    }

    const home = $('#st-home').value.trim();
    if (home) {
      body.home = { size: home };
    }

    await call('PATCH', profilePath(), body);
    await openProfile(state.profile.name);
    toast('Saved', 'ok');
  },

  async 'toggle-suspend'() {
    await call('PATCH', profilePath(), { suspended: !state.profile.suspended });
    await openProfile(state.profile.name);
  },

  async 'delete-profile'() {
    const name = esc(state.profile.name);
    const html = `<h2>Delete ${name}?</h2><p>Its pods, services, secrets and volumes (home directory included) are removed. This can't be undone.</p><div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn danger" id="del-yes">Delete</button></div>`;

    modal(html, (card) => {
      card.querySelector('#del-yes').onclick = async () => {
        await call('DELETE', profilePath());
        closeModal();
        toast('Profile deleted', 'ok');
        actions.back();
      };
    });
  },

  async 'add-user'() {
    try {
      await api('POST', '/api/users', { email: $('#us-email').value.trim(), password: $('#us-password').value });
    } catch (error) {
      $('#us-error').textContent = error.message;
      return;
    }

    await reloadUsers();
    toast('Admin added', 'ok');
  },
};

// ---------------------------------------------------------------------------
// Wiring the controls of the view on the page
// ---------------------------------------------------------------------------

/** Run *handler* for each element in the view matching *selector*. */
function each(selector, handler) {
  $('#view').querySelectorAll(selector).forEach(handler);
}

/** One allowlist edit to the open profile, then show the result. */
async function editAllowlist(op, entry, flag) {
  await call('POST', profilePath('/allowlist'), { op, entry, flag });
  await openProfile(state.profile.name);
}

/** The language switches, the package form and ✕ buttons, and the extension form and buttons of a profile's view. */
function bindLanguagesAndExtensions() {
  each('[data-lang]', (toggle) => {
    toggle.onchange = async () => {
      try {
        await setLanguage(toggle.dataset.lang, toggle.checked);
      } catch {
        // The toast said why. Put the switch back where it was drawn, as the
        // allowlist's switches are: left flipped, it counts as unsaved input.
        toggle.checked = toggle.defaultChecked;
      }
    };
  });

  const extensionId = $('#ex-id');
  if (extensionId) {
    extensionId.onkeydown = (event) => {
      if (event.key === 'Enter') {
        actions['ext-add']();
      }
    };
  }

  each('[data-ext-remove]', (button) => {
    button.onclick = async () => {
      try {
        await editExtensions('remove', button.dataset.extRemove);
      } catch (error) {
        toast(error.message, 'error');
      }
    };
  });

  const packageName = $('#pk-name');
  if (packageName) {
    packageName.onkeydown = (event) => {
      if (event.key === 'Enter') {
        actions['pkg-add']();
      }
    };
  }

  each('[data-pkg-remove]', (button) => {
    button.onclick = async () => {
      try {
        await editPackages('remove', [button.dataset.pkgRemove]);
      } catch (error) {
        toast(error.message, 'error');
      }
    };
  });
}

/** A one-line summary per profile of what a bulk edit did. */
function describeBulkResult(op, entry, results) {
  const lines = Object.entries(results).map(([profile, outcome]) => `  ${profile.padEnd(24)} ${outcome}`);
  return `${op} ${entry}\n` + lines.join('\n');
}

/** Wire up the controls of the view just rendered. */
function bind() {
  const buttons = [...$('#view').querySelectorAll('[data-act]'), ...$('#top-actions').querySelectorAll('[data-act]')];
  buttons.forEach((button) => {
    button.onclick = () => actions[button.dataset.act](button);
  });

  each('[data-open]', (row) => {
    row.onclick = () => openProfile(row.dataset.open, 'languages');
  });

  bindLanguagesAndExtensions();

  each('[data-tab]', (tab) => {
    tab.onclick = () => {
      state.tab = tab.dataset.tab;
      render();
      if (state.tab === 'blocked') actions['refresh-blocked']();
    };
  });

  const search = $('#q');
  if (search) {
    search.oninput = () => {
      // Rendering replaces the box; keep the focus and the caret where they were.
      state.query = search.value;
      const caret = search.selectionStart;
      render();
      $('#q').focus();
      $('#q').setSelectionRange(caret, caret);
    };
  }

  const since = $('#since');
  if (since) {
    since.onchange = () => {
      state.since = since.value;
      actions['refresh-blocked']();
    };
  }

  const entry = $('#al-entry');
  if (entry) {
    entry.onkeydown = (event) => {
      if (event.key === 'Enter') actions['al-add']();
    };
  }

  each('[data-toggle]', (toggle) => {
    toggle.onchange = async () => {
      try {
        await editAllowlist(toggle.checked ? 'enable' : 'disable', toggle.dataset.toggle, toggle.dataset.flag);
      } catch {
        // The toast said why. Put the switch back where it was drawn: left
        // flipped it shows a state the allowlist doesn't have, and counts as
        // unsaved input, so the refresh would never redraw it.
        toggle.checked = toggle.defaultChecked;
      }
    };
  });

  each('[data-remove]', (button) => {
    button.onclick = () => editAllowlist('remove', button.dataset.remove, button.dataset.flag);
  });

  // The names come from logs the sandbox produced: pre-fill the form for a
  // look, don't allow anything in one click.
  each('[data-allow]', (button) => {
    button.onclick = () => {
      state.prefill = button.dataset.allow;
      state.prefillFlag = button.dataset.flag || '';
      state.tab = 'allowlist';
      render();
      $('#al-entry').focus();
    };
  });

  // The same for On your network: the name, and the address for someone
  // who knows it to type (or the one a moved name now has, to look at).
  each('[data-allow-lan]', (button) => {
    button.onclick = () => {
      state.lanPrefill = { name: button.dataset.allowLan, address: '' };
      state.tab = 'allowlist';
      render();
      $('#lan-address').focus();
    };
  });

  each('[data-lan-move]', (button) => {
    button.onclick = () => {
      state.lanPrefill = { name: button.dataset.lanMove, address: button.dataset.address };
      render();
      $('#lan-address').focus();
    };
  });

  each('[data-lan-remove]', (button) => {
    button.onclick = async () => {
      const answer = await call('POST', profilePath('/lan'), { op: 'remove', name: button.dataset.lanRemove });
      await openProfile(state.profile.name);
      toast(answer.message, 'ok');
    };
  });

  each('[data-bulk]', (button) => {
    button.onclick = async () => {
      const op = button.dataset.bulk;
      const entry = $('#bk-entry').value.trim();
      const flag = $('#bk-flag').value;

      let results;
      try {
        results = await api('POST', '/api/allowlist/bulk', { op, entry, flag });
      } catch (error) {
        $('#bk-error').textContent = error.message;
        return;
      }

      // Kept in the state and drawn now: the form still holds what was typed,
      // so the refresh of the profile list wouldn't redraw it. Drawn with the
      // same entry and access, the form is clean again for the next refresh.
      state.bulkEntry = entry;
      state.bulkFlag = flag;
      state.bulkResult = describeBulkResult(op, entry, results);
      render();
      loadProfiles();
    };
  });

  each('[data-reset-totp]', (button) => {
    button.onclick = async () => {
      await call('POST', `/api/users/${encodeURIComponent(button.dataset.resetTotp)}/reset-totp`, {});
      await reloadUsers();
      toast('2FA reset; they enrol again at next sign-in', 'ok');
    };
  });

  each('[data-del-user]', (button) => {
    button.onclick = async () => {
      await call('DELETE', `/api/users/${encodeURIComponent(button.dataset.delUser)}`);
      await reloadUsers();
    };
  });
}

// ---------------------------------------------------------------------------
// The navigation, and start-up
// ---------------------------------------------------------------------------

document.querySelectorAll('.nav-item').forEach((button) => {
  button.onclick = async () => {
    state.view = button.dataset.view;
    if (state.view === 'users') state.users = await call('GET', '/api/users');
    if (state.view !== 'profile') state.profile = null;

    render();
    if (state.view === 'profiles' || state.view === 'bulk') loadProfiles();
  };
});

// A click outside the dialog closes it.
$('#modal').onclick = (event) => {
  if (event.target === $('#modal')) closeModal();
};

/**
 * Keep the view current, without ever redrawing over a form someone is
 * filling in. Its requests say they are the poll's: they aren't activity.
 *
 * A profile's extensions installing in the background are followed here
 * too: while a form on the Extensions tab holds input, their status alone
 * is redrawn.
 */
function poll() {
  if (state.view === 'profiles') {
    loadProfiles(true);
    return;
  }

  if (state.view === 'profile' && state.profile) {
    const name = state.profile.name;
    const stillShown = () => state.view === 'profile' && state.profile && state.profile.name === name;

    api('GET', `/api/profiles/${name}`, undefined, { poll: true })
      .then((profile) => {
        // Another profile was opened while this was on its way.
        if (!stillShown()) {
          return;
        }

        state.profile = profile;
        if (state.tab !== 'blocked' && !formDirty()) {
          render();
          return;
        }

        const status = $('#ext-status');
        if (status) {
          status.outerHTML = extensionStatus(profile.extensionStatus);
        }
      })
      .catch((error) => {
        // Deleted elsewhere (another admin, kubectl): back to the list, said
        // once, rather than a failed refresh every ten seconds. Other
        // failures of a background refresh pass quietly; the next one retries.
        if (error.status === 404 && stillShown()) {
          toast(`Profile ${name} no longer exists`, 'error');
          state.view = 'profiles';
          state.profile = null;
          render();
          loadProfiles(true);
        }
      });
  }
}

/**
 * Start up, or start again after signing in: the sign-in form, or the
 * profiles and a refresh every ten seconds. Only one refresh ever runs.
 */
async function boot() {
  state.me = await api('GET', '/api/me');
  if (!state.me) {
    return showLogin();
  }

  showApp();
  $('#me').textContent = state.me.email;
  $('#ns').textContent = `namespace ${state.me.namespace}`;

  // The languages and tools never change while the admin runs: read once.
  // Without them (the toast says why) the page still works, with nothing to
  // pick from when making a profile.
  state.catalogue = await call('GET', '/api/languages').catch(() => []);

  state.view = 'profiles';
  render();
  await loadProfiles();

  clearInterval(state.poller);
  state.poller = setInterval(poll, 10000);
}

boot();
