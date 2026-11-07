/**
 * `window.hardcode`, the page's one way of getting anything done.
 *
 * Built on Tauri's global API, it runs the `hardcode` command line with
 * `--json` for each operation, so the app has no logic of its own: whatever
 * the page shows, the CLI said. Long commands (start, rebuild, delete) stream
 * their output to the page's console instead.
 */
'use strict';

(() => {
  const tauri = window.__TAURI__;
  const { invoke } = tauri.core;
  const { listen } = tauri.event;
  const dialog = tauri.dialog;
  const opener = tauri.opener;

  let settings = {};
  let home = ''; // the folder the CLI keeps its profiles in, to show
  let profile = null; // the profile shown; chosen by home:get
  let state = null; // the last `show` result, for finding the entry on a line
  let sequence = 0;

  const outputCallbacks = [];
  const doneCallbacks = [];
  const pending = new Map(); // stream id -> {resolve, reject}

  // How the proxy stands with the allowlist when the CLI couldn't say.
  const PROXY_UNKNOWN = { reloading: false, due: null, behind: false, error: '' };

  // ---------------------------------------------------------------------------
  // Streams
  // ---------------------------------------------------------------------------

  /**
   * The command name in a stream's id. The id is the name plus a sequence
   * number ("start#3"), so two runs of the same command can't take each
   * other's result; the page sees both.
   */
  const nameOf = (id) => id.split('#')[0];

  listen('cli-output', (event) => {
    const { id, line } = event.payload;
    outputCallbacks.forEach((callback) => callback({ name: nameOf(id), id, line }));
  });

  listen('cli-done', (event) => {
    const { id, code } = event.payload;
    doneCallbacks.forEach((callback) => callback({ name: nameOf(id), id, code }));

    const waiter = pending.get(id);
    if (!waiter) {
      return;
    }

    pending.delete(id);
    if (code === 0) {
      waiter.resolve(code);
    } else {
      waiter.reject(new Error(`${id} failed (exit ${code}); see the console`));
    }
  });

  /** Run a long command, streamed to the console; resolves when it exits. */
  function stream(name, args, forProfile = profile) {
    const id = `${name}#${++sequence}`;

    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });

      invoke('cli_stream', { id, args: ['-p', forProfile, ...args] }).catch((error) => {
        pending.delete(id);
        reject(error);
      });
    });
  }

  // ---------------------------------------------------------------------------
  // One-shot CLI calls
  // ---------------------------------------------------------------------------

  /** The CLI's last line of complaint, as an error the page can show. */
  function failure(result, args) {
    const lastLine = (result.stderr || result.stdout || '').trim().split('\n').pop();
    const text = lastLine || `hardcode ${args.join(' ')} failed`;
    return new Error(text.replace(/^error:\s*/, ''));
  }

  /** Run the CLI with *args* and return what it printed; its complaint when it fails. */
  async function raw(args) {
    const result = await invoke('cli_run', { args });
    if (result.code !== 0) {
      throw failure(result, args);
    }

    return result.stdout;
  }

  /** One CLI call for the current profile (or every profile), parsed from --json. */
  async function cli(args, { forProfile = profile, scope = 'profile' } = {}) {
    // Before the first profile there is none to name: a call that needs one
    // is a mistake here, not a null for Tauri to refuse.
    if (!forProfile) {
      throw new Error(`hardcode ${args.join(' ')} needs a profile, and there is none yet`);
    }

    const everyProfile = scope === 'all' ? ['--all'] : [];
    const full = ['--json', '-p', forProfile, ...everyProfile, ...args];

    return JSON.parse(await raw(full));
  }

  /** Remember the profile in the app's settings file. */
  async function save() {
    settings = { ...settings, profile };
    await invoke('settings_save', { settings });
  }

  /**
   * The profile's state, as the page wants it. `langs` is the catalogue with
   * the profile's choice and what its image has; `extensions` every VS Code
   * extension and where it comes from; `packages` its own Ubuntu packages
   * and `dockerfile` its own Dockerfile steps ({path, exists}); `image`,
   * `rebuildNeeded` and `rebuildReasons` whether the image was built with
   * the profile's languages, packages and steps, and what differs;
   * `devices` each device of this computer the VS Code container may be
   * given ({key, name, summary, warning, enabled, available, reason}).
   */
  async function refresh() {
    state = await cli(['show']);

    return {
      home,
      profile,
      profiles: state.profiles,
      env: state.env,
      vscodePort: state.vscodePort,
      allowlist: state.allowlist,
      lan: state.lan,
      forwards: state.forwards,
      mounts: state.mounts,
      langs: state.langs,
      extensions: state.extensions,
      packages: state.packages,
      dockerfile: state.dockerfile,
      image: state.image,
      rebuildNeeded: state.rebuildNeeded,
      rebuildReasons: state.rebuildReasons,
      docker: state.docker,
      devices: state.devices,
      busy: null,
    };
  }

  /** The new state, with what the CLI said each profile did. */
  async function withResults(response) {
    return { ...(await refresh()), lastResults: response.results };
  }

  /**
   * An entry of the full list, by its id, as CLI arguments: its text and, for an SSH entry,
   * the flag that tells it apart from the host's HTTPS entry. `--` keeps a
   * value that starts with "-" from being read as an option.
   */
  function entryArgs(id) {
    const item = state && state.allowlist.find((entry) => entry.id === id);
    if (!item) {
      throw new Error('no such entry');
    }

    return ['--', item.entry, ...(item.flag === 'ssh' ? ['ssh'] : [])];
  }

  /** The options of a call that applies to *scope*: 'profile', or 'all' profiles. */
  const scoped = (scope) => ({ scope });

  // ---------------------------------------------------------------------------
  // The channels the page calls
  // ---------------------------------------------------------------------------

  const handlers = {
    // -- the home and its profiles

    /**
     * Where the profiles are, and which one to show: the one last used,
     * else the first there is. With none yet, `profile` is null and
     * `catalogue` is every language and tool, to choose the first
     * profile's from.
     */
    async 'home:get'() {
      settings = await invoke('settings_load');
      const listed = JSON.parse(await raw(['--json', 'profiles']));
      home = listed.home;

      // The one shown stays; else the one last used, default, or the first.
      const names = listed.profiles.map((item) => item.name);
      if (!names.includes(profile)) {
        const choices = [settings.profile, 'default', names[0]];
        profile = choices.find((name) => names.includes(name)) || null;
      }

      if (profile) {
        return { home, version: listed.version, profile, catalogue: null };
      }

      const catalogue = JSON.parse(await raw(['--json', 'languages'])).languages;
      return { home, version: listed.version, profile: null, catalogue };
    },

    /** Show the home in the file manager, to edit a profile's files by hand. */
    async 'home:open'() {
      await opener.revealItemInDir(home);
    },

    'state:get': refresh,

    async 'profiles:select'(name) {
      profile = name;
      await save();
      return refresh();
    },

    // A new profile, built around exactly *languages* (keys of languages and
    // tools; none is an empty list). Without a list it keeps the source's.
    async 'profiles:create'(name, from, languages) {
      const source = from && from !== '_template' ? ['--from', from] : [];
      const chosen = Array.isArray(languages) ? [`--lang=${languages.join(',')}`] : [];

      // Not for a profile (there is none before the first): no -p.
      await raw(['profile', 'add', ...source, ...chosen, '--', name]);

      profile = name;
      await save();
      return refresh();
    },

    // Another profile's languages, for the new-profile dialog's "copy of".
    async 'profiles:languages'(name) {
      return (await cli(['languages'], { forProfile: name })).languages;
    },

    async 'profiles:remove'(name) {
      const confirmed = await dialog.ask(
        'Removes its containers and every volume (home directory, /shared, pulled images, proxy CA), its VS Code image, and its settings and allowlist.',
        { title: `Delete profile "${name}"?`, kind: 'warning', okLabel: `Delete "${name}"`, cancelLabel: 'Cancel' },
      );
      if (!confirmed) {
        return refresh();
      }

      await stream(`delete ${name}`, ['profile', 'remove', '--yes', '--', name], name);

      // The page asks home:get what to show next: another profile, or none.
      if (profile === name) {
        profile = null;
      }
      await save();
      return null;
    },

    // -- the allowlist. A running proxy reloads with each change by itself,
    //    a few seconds after the last; the results say when, and the page
    //    tells the user.

    async 'allowlist:add'(entry, flag, scope) {
      const response = await cli(['allow', '--', entry, ...(flag ? [flag] : [])], scoped(scope));
      return withResults(response);
    },

    async 'allowlist:setEnabled'(id, enabled, scope) {
      const response = await cli(['allowlist', enabled ? 'enable' : 'disable', ...entryArgs(id)], scoped(scope));
      return withResults(response);
    },

    async 'allowlist:remove'(id, scope) {
      const response = await cli(['allowlist', 'remove', ...entryArgs(id)], scoped(scope));
      return withResults(response);
    },

    // Every change waiting, made now: the proxy's allowlist, and the
    // containers waiting to be recreated. The CLI's results.
    async 'changes:apply'(scope) {
      return (await cli(['apply'], scoped(scope))).results;
    },

    // -- names on your network: each lets one name reach one address there,
    //    which the proxy refuses otherwise. Never added without asking.

    // Each name, looked up again: {name, address, problem, resolves, state,
    // moveTo}, state "ok", "moved" (it resolves only elsewhere now) or
    // "unresolved", and moveTo the address a moved name may move to ("" when
    // none: never this computer's, nor one in the sandbox's own network).
    async 'lan:list'() {
      return (await cli(['lan'])).lan;
    },

    // What a name resolves to from here: {name, resolves, address, pinned,
    // refused}, address being the one on your network it could be let reach,
    // and refused why there is none when it resolves inside (this computer,
    // the sandbox's own network): such an address is never offered.
    async 'lan:check'(name) {
      return cli(['lan', 'check', '--', name]);
    },

    // Ask whether *name* may reach *address*, and add it if so: the new
    // state, or null when the answer is no. Only an address the CLI offered
    // is asked about, so the question never calls one it refuses "on your
    // network".
    async 'lan:offer'(name, address, scope) {
      const confirmed = await dialog.ask(
        `${name} resolves to ${address}, on your network. Let it reach that address? Nothing else on your network opens.`,
        { title: `Let ${name} reach ${address}?`, kind: 'warning', okLabel: 'Let it reach that address', cancelLabel: 'Not now' },
      );
      if (!confirmed) {
        return null;
      }

      return withResults(await cli(['lan', 'add', '--', name, address], scoped(scope)));
    },

    async 'lan:remove'(name, scope) {
      return withResults(await cli(['lan', 'remove', '--', name], scoped(scope)));
    },

    // -- forwards and mounts. The containers they recreate are recreated a
    //    few seconds after the profile's last such change; the results say
    //    which, and when.

    async 'forwards:add'(port, host, target) {
      const differentTarget = target && target !== port ? [String(target)] : [];
      return withResults(await cli(['forward', '--', String(port), host, ...differentTarget]));
    },

    async 'forwards:remove'(port) {
      return withResults(await cli(['unforward', '--', String(port)]));
    },

    async 'mounts:pickFolder'() {
      return (await dialog.open({ directory: true, title: 'Folder to mount into the VS Code container' })) || '';
    },

    async 'mounts:add'(local, remote, readOnly, scope) {
      return withResults(await cli(['mount', ...(readOnly ? ['--ro'] : []), '--', local, remote], scoped(scope)));
    },

    async 'mounts:remove'(remote, scope) {
      return withResults(await cli(['unmount', '--', remote], scoped(scope)));
    },

    // -- languages, extensions, the image and the Docker daemon

    // A language's allowlist block changes, and the proxy reloads, at once;
    // a running VS Code container installs or removes its extensions in the
    // background within seconds; the toolchain waits for langs:apply.
    async 'langs:set'(key, enabled, scope) {
      const response = await cli(
        [enabled ? 'enable' : 'disable', '--no-build', '--', String(key).toLowerCase()],
        scoped(scope),
      );
      return withResults(response);
    },

    async 'langs:apply'(scope) {
      await stream('rebuild vscode', [...(scope === 'all' ? ['--all'] : []), 'rebuild']);
      return refresh();
    },

    // The profile's own extensions. A running VS Code container installs (or
    // removes) them in the background within seconds; a stopped one as it starts.
    async 'extensions:add'(id, scope) {
      return withResults(await cli(['extension', 'add', '--', id], scoped(scope)));
    },

    async 'extensions:remove'(id, scope) {
      return withResults(await cli(['extension', 'remove', '--', id], scoped(scope)));
    },

    // The profile's own Ubuntu packages. The image installs them, so they
    // arrive with its next rebuild (langs:apply), which is due until then.
    async 'packages:add'(names, scope) {
      return withResults(await cli(['package', 'add', '--', ...names], scoped(scope)));
    },

    async 'packages:remove'(name, scope) {
      return withResults(await cli(['package', 'remove', '--', name], scoped(scope)));
    },

    // The profile's own Dockerfile steps: {path, folder, exists, dockerfile,
    // starter, digest, base (HardCode's Dockerfile), basePath, image,
    // rebuildNeeded, rebuildReasons}.
    async 'image:get'() {
      return cli(['image']);
    },

    // Write the profile's own Dockerfile steps; the next rebuild (langs:apply) builds them.
    async 'image:save'(text) {
      return withResults(await cli(['image', 'save', '--', text]));
    },

    // How far the running VS Code container has got with the profile's
    // extensions: {state: "installing" | "done", total, installed, pending,
    // refused, failed, updated}, the ids in lists; null while it isn't
    // running. It asks the container itself, so the page calls it when
    // Extensions opens, and while extensions are installing.
    async 'extensions:status'() {
      return (await cli(['extensions'])).status;
    },

    async 'docker:set'(enabled, scope) {
      return withResults(await cli(['docker', enabled ? 'on' : 'off'], scoped(scope)));
    },

    // A device of this computer for the profile's VS Code container: kvm,
    // gpu or nvidia. The page asks first, with what the device gives code
    // in the sandbox, so on is passed --yes; off asks nothing. One profile
    // at a time, always.
    async 'device:set'(key, enabled) {
      const confirmed = enabled ? ['--yes'] : [];
      return withResults(await cli(['device', enabled ? 'on' : 'off', ...confirmed, '--', key]));
    },

    // -- the stack

    // What runs, or why that can't be told: the daemon isn't running, the
    // CLI failed. Only a missing docker command reads as "docker not found".
    // `extensions` is how far the running VS Code container has got with the
    // profile's extensions ({state, installed, total}), null while it isn't
    // running. `proxy` is how the proxy stands with the allowlist
    // ({reloading, due, behind, error}): it reloads with a change by itself.
    // `changes` is what waits to recreate containers ({changes, services,
    // token, due, applying, undone, error}), and the last changes undone.
    async 'stack:status'() {
      try {
        const response = await cli(['ps']);
        return {
          services: response.services,
          docker: response.docker !== false,
          error: response.error || '',
          extensions: response.extensions,
          proxy: response.proxy || { ...PROXY_UNKNOWN },
          changes: response.changes || null,
        };
      } catch (error) {
        return {
          services: [],
          docker: null,
          error: String(error.message || error),
          extensions: null,
          proxy: { ...PROXY_UNKNOWN },
          changes: null,
        };
      }
    },

    'stack:start': () => stream('start', ['up']),
    'stack:stop': () => stream('stop', ['stop']),
    'stack:restart': () => stream('restart', ['restart']),

    async 'stack:rebuild'() {
      await stream('rebuild', ['build']);
      return stream('start', ['up']);
    },

    async 'stack:reset'() {
      const confirmed = await dialog.ask(
        "Removes the containers and every volume: the home directory, /shared, pulled images and the proxy CA. The profile's settings and the images you built are kept.",
        {
          title: `Delete profile "${profile}"'s sandbox and all its data?`,
          kind: 'warning',
          okLabel: 'Delete everything',
          cancelLabel: 'Cancel',
        },
      );
      if (!confirmed) {
        return null;
      }

      return stream('reset', ['down', '-v']);
    },

    // -- VS Code, and what the proxy refused

    // The URL with its token; the CLI's error (the VS Code container isn't running,
    // the token isn't written yet) when there is none.
    async 'vscode:url'() {
      return (await cli(['open', '--no-browser'])).results[0].data.url;
    },

    // The page passes the URL it already has; without one, it is asked for.
    // A new token, once the user confirms: every copy of the old one stops
    // working, and VS Code restarts a few seconds later. The new state with
    // the CLI's results; null when they don't.
    async 'vscode:port'(port) {
      const response = await cli(['port', '--', String(port)]);
      return withResults(response);
    },

    async 'vscode:rotate'() {
      const confirmed = await dialog.ask(
        'Every copy of the current token stops working (a screenshot, a shared screen, a browser history), and VS Code restarts in a few seconds: open windows need the new URL.',
        { title: `A new VS Code token for "${profile}"?`, kind: 'warning', okLabel: 'New token', cancelLabel: 'Cancel' },
      );
      if (!confirmed) {
        return null;
      }

      return withResults(await cli(['token', 'rotate']));
    },

    async 'vscode:open'(url) {
      await opener.openUrl(url || (await handlers['vscode:url']()));
      return true;
    },

    async 'stack:logs'(service, lines) {
      return (await cli(['logs', '--tail', String(lines), '--', service])).logs;
    },

    async 'traffic:blocked'(since) {
      return (await cli(['blocked', '--since', since || '30m'])).blocked;
    },

    // -- updates (a release's; a development build never has one)

    /** A newer release of the app ({version, current, notes}), or null. */
    'update:check': () => invoke('update_check'),

    /** Install the release the check found, and start it: this window closes. */
    'update:install': () => invoke('update_install'),

    /** The release's page, in the browser. */
    async 'update:notes'(url) {
      await opener.openUrl(url);
    },

    /** A page of HardCode's docs, in the browser; no other address. */
    async 'docs:open'(url) {
      if (!String(url).startsWith('https://github.com/hardcode-app/hardcode/blob/main/docs/')) {
        throw new Error(`not a page of HardCode's docs: ${url}`);
      }
      await opener.openUrl(url);
    },

    // -- plumbing

    'shell:open': async () => false,
    'stream:cancel': (id) => invoke('cli_cancel', { id }),
    'smoke:mode': () => invoke('smoke_mode'),
    'smoke:done': (views, errors, update) => invoke('smoke_done', { views, errors, profile, update }),
  };

  window.hardcode = {
    /** Call a channel with *args*; a promise of its answer. */
    invoke(channel, ...args) {
      const handler = handlers[channel];
      if (!handler) {
        return Promise.reject(new Error(`unknown channel ${channel}`));
      }

      return handler(...args);
    },

    /** Have *callback* called with each line a streamed command prints. */
    onOutput(callback) {
      outputCallbacks.push(callback);
    },

    /** Have *callback* called when a streamed command exits. */
    onDone(callback) {
      doneCallbacks.push(callback);
    },

    platform: navigator.platform,
  };
})();
