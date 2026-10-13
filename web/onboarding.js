'use strict';
// Invitation secrets stay out of HTTP request URLs, referrers and access logs.
const invite = new URLSearchParams(location.hash.slice(1)).get('invite');
const field = document.getElementById('invite-code');
if (field && invite) field.value = invite;

// Only a same-origin decision route survives login. Never store bearer fragments.
if (/^#runs\/[a-f0-9]{12,32}\/[a-f0-9]{12,32}$/.test(location.hash)) {
  try { sessionStorage.setItem('raven-return-decision', JSON.stringify({route:location.hash, savedAt:Date.now()})); }
  catch {} // Storage may be disabled; ordinary sign-in still works.
}
