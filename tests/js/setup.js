// Date arithmetic in the UI is local-time based; pin the test process so
// expected strings do not depend on the machine's timezone.
process.env.TZ = 'UTC';
