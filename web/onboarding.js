'use strict';
// Invitation secrets stay out of HTTP request URLs, referrers and access logs.
const invite = new URLSearchParams(location.hash.slice(1)).get('invite');
const field = document.getElementById('invite-code');
if (field && invite) field.value = invite;
