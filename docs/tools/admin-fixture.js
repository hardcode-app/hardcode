/**
 * Loaded before the admin page's app.js when it is rendered for the
 * documentation screenshots: `fetch` answers from fixture data
 * (fixture-data.json, written by fixture-data.py) instead of the API, and
 * `?view=` drives the page to the state being documented:
 *
 *   login | enrol | totp | profiles | profile-languages | profile-extensions |
 *   profile-allowlist | profile-blocked | profile-access | profile-settings |
 *   bulk | users | new-profile | raw-allowlist | delete
 */
'use strict';

(() => {
  const params = new URLSearchParams(location.search);
  const view = params.get('view') || 'profiles';
  const signedIn = !['login', 'enrol', 'totp'].includes(view);
  const fixture = fetch('/fixture-data.json').then((response) => response.json());

  const you = { email: 'you@example.com', namespace: 'sandbox' };
  let me = signedIn ? you : null;

  const json = (body, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

  // The admin API, answered from the fixture.
  window.fetch = async (url, options = {}) => {
    const data = await fixture;
    const path = String(url).split('?')[0];
    const method = (options.method || 'GET').toUpperCase();

    if (path === '/api/me') return json(me);

    if (path === '/api/login') {
      const enrolment = { next: 'enrol', otpauth: data.otpauth, secret: data.secret, qr: data.qr };
      return json(view === 'enrol' ? enrolment : { next: 'totp' });
    }

    if (path === '/api/totp') {
      me = you;
      return json({ ok: true, email: me.email });
    }

    if (path === '/api/logout') {
      me = null;
      return json({ ok: true });
    }

    if (path === '/api/profiles' && method === 'GET') return json(data.profiles);
    if (path === '/api/template') return json({ allowlist: data.template });
    if (path === '/api/languages') return json(data.catalogue);
    if (path.endsWith('/extensions')) return json({ changed: true, message: 'added extension' });
    if (path === '/api/allowlist/bulk') return json({ 'team-a': 'changed', 'team-b': 'changed', research: 'unchanged' });
    if (path.endsWith('/blocked')) return json(data.blocked);
    if (path.startsWith('/api/profiles/')) return json(data.profile);
    if (path === '/api/users') return json(data.users);

    return json({ ok: true });
  };

  // -- drive the page to the view being documented

  const $ = (selector) => document.querySelector(selector);

  const waitFor = (condition) =>
    new Promise((resolve) => {
      const timer = setInterval(() => {
        if (condition()) {
          clearInterval(timer);
          resolve();
        }
      }, 50);
    });

  /** The sign-in form, filled in; for enrol and totp, the password step done too. */
  async function signingIn() {
    await waitFor(() => !$('#login').classList.contains('hidden'));
    $('#lg-email').value = 'you@example.com';
    $('#lg-password').value = 'correct horse battery staple';
    if (view === 'login') return;

    $('#lg-submit').click();
    await waitFor(() => !$('#login-step-totp').classList.contains('hidden'));
    $('#lg-code').value = '492817';
  }

  async function navigation() {
    $(`.nav-item[data-view="${view}"]`).click();

    if (view === 'bulk') {
      await waitFor(() => $('#bk-entry'));
      $('#bk-entry').value = 'github.com/ourcompany';
      $('[data-bulk="add"]').click();
      await waitFor(() => $('#bk-result').textContent);
    } else {
      await waitFor(() => $('[data-reset-totp]'));
    }
  }

  /** team-a, opened at the tab being documented, with its dialog open for raw-allowlist and delete. */
  async function oneProfile() {
    $('[data-open="team-a"]').click();
    await waitFor(() => $('[data-tab]'));

    const tabs = {
      'profile-languages': 'languages',
      'profile-extensions': 'extensions',
      'profile-blocked': 'blocked',
      'profile-access': 'access',
      'profile-settings': 'settings',
      delete: 'settings',
    };
    const tab = tabs[view] || 'allowlist';
    $(`[data-tab="${tab}"]`).click();
    await waitFor(() => $('.tab.active').dataset.tab === tab && (tab !== 'blocked' || $('#view tbody tr td.mono')));

    // The allowlist scrolled to its first language block, the end of the
    // section before it still in view.
    if (view === 'profile-allowlist') {
      const scroller = $('#view');
      const block = [...scroller.querySelectorAll('tr.section-row')].find((row) => row.textContent.includes('comes with the language'));
      scroller.scrollTop += block.getBoundingClientRect().top - scroller.getBoundingClientRect().top - 240;
    }

    if (view === 'raw-allowlist') $('[data-act="al-raw"]').click();
    if (view === 'delete') $('[data-act="delete-profile"]').click();
  }

  window.addEventListener('load', async () => {
    if (!signedIn) {
      await signingIn();
      return;
    }

    await waitFor(() => $('[data-open]'));
    if (view === 'profiles') return;

    if (view === 'bulk' || view === 'users') {
      await navigation();
      return;
    }

    // A new profile from the template, with two languages picked besides
    // the tools it starts with.
    if (view === 'new-profile') {
      $('[data-act="new-profile"]').click();
      $('#np-name').value = 'team-c';
      $('#np-host').value = 'team-c.code.example.com';
      $('[data-pick="python"]').checked = true;
      $('[data-pick="rust"]').checked = true;
      return;
    }

    await oneProfile();
  });
})();
