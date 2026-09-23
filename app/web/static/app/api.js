// Fetch helpers, unchanged in behavior from the old app.js. Every
// mutating call sends Content-Type: application/json and
// credentials: 'same-origin' (cookies), per the HTTP API contract.
//
// Deliberately dumb about auth: a 401 here is just a status code like
// any other. What it *means* differs by call site (a login endpoint's
// 401 is "wrong passphrase/code", shown inline; an authenticated
// endpoint's 401 is "the session died", which calls store.js's
// forceLogout()) so that decision stays with the caller, exactly as
// the old app.js only called goToLogin() from specific call sites
// rather than from inside apiCall() itself.
export async function apiCall(path, options) {
  let res;
  try {
    res = await fetch(path, options);
  } catch {
    return { status: 0, ok: false, data: null };
  }
  let data = null;
  try {
    data = await res.json(); // 202/204 responses simply have no body
  } catch {
    // ignore
  }
  return { status: res.status, ok: res.ok, data };
}

export function apiGet(path) {
  return apiCall(path, { credentials: 'same-origin' });
}

export function apiPost(path, body) {
  return apiCall(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}
