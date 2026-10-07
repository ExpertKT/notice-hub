const mode = process.env.FAKE_CODEBUDDY_MODE || 'ok';
if (mode === 'exit') { process.stderr.write('fake failure'); process.exit(7); }
if (mode === 'invalid') { process.stderr.write('fake invalid'); process.stdout.write('not json'); process.exit(0); }
process.stdout.write(JSON.stringify({items: []}));
