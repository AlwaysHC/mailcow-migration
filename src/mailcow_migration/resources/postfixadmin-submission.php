<?php
try {
    $request = json_decode(stream_get_contents(STDIN), true, 512, JSON_THROW_ON_ERROR);
    if ($request['action'] !== 'inspect') throw new RuntimeException('Unsupported submission inspection action');
    $type = command(array('postconf', '-h', 'smtpd_sasl_type'));
    if ($type !== 'dovecot') throw new RuntimeException('Source SMTP authentication needs the supported Dovecot adapter');
    $queue = command(array('postconf', '-h', 'queue_directory'));
    $base = command(array('doveconf', '-h', 'base_dir'));
    if ($queue === '' || $queue[0] !== '/' || $base === '' || $base[0] !== '/') {
        throw new RuntimeException('Source authentication directories must be absolute');
    }
    if (command(array('doveconf', '-h', 'auth_default_realm')) !== '' ||
        command(array('doveconf', '-h', 'auth_master_user_separator')) !== '' ||
        !in_array(command(array('doveconf', '-h', 'auth_username_format')), array('%u', '%Lu'), true)) {
        throw new RuntimeException('Short/master logins or username rewriting need a source submission adapter');
    }
    $master = preg_replace('/^[ \t]*#.*$/m', '', fileText('/etc/postfix/master.cf'));
    $master = preg_replace('/\r?\n[ \t]+/', ' ', $master);
    if (preg_match('/(?:^|\s)-o(?:[^\s]|\s*\{)/', $master)) {
        throw new RuntimeException('Source service options need an adapter for attached or braced override syntax');
    }
    if (preg_match_all('/-o\s+smtpd_sasl_type\s*=\s*(\S+)/', $master, $matches)) {
        foreach ($matches[1] as $value) if ($value !== 'dovecot') {
            throw new RuntimeException('A Postfix service uses another SMTP authentication backend');
        }
    }
    $paths = array(command(array('postconf', '-h', 'smtpd_sasl_path')));
    if (preg_match_all('/-o\s+smtpd_sasl_path\s*=\s*(\S+)/', $master, $matches)) {
        $paths = array_merge($paths, $matches[1]);
    }
    $dovecot = command(array('doveconf', '-n'));
    if (preg_match_all('/^service auth\s*\{([\s\S]*?)^\}/m', $dovecot, $authentication) !== 1) {
        throw new RuntimeException('Expected one configured Dovecot authentication service');
    }
    preg_match_all('/^[ \t]*unix_listener\s+([^\s{}]+)\s*\{/m', $authentication[1][0], $listeners);
    $sockets = array();
    foreach ($listeners[1] as $path) {
        $resolved = realpath($path[0] === '/' ? $path : $base . '/' . $path);
        if ($resolved !== false) $sockets[] = $resolved;
    }
    $verified = array();
    foreach (array_unique($paths) as $path) {
        if ($path === '' || strpos($path, '..') !== false || preg_match('/[\r\n\x00]/', $path)) {
            throw new RuntimeException('Unsupported SMTP authentication socket path');
        }
        $resolved = realpath($path[0] === '/' ? $path : $queue . '/' . $path);
        if ($resolved === false || !in_array($resolved, $sockets, true) || filetype($resolved) !== 'socket') {
            throw new RuntimeException('SMTP authentication does not resolve to a live configured Dovecot socket');
        }
        $verified[] = $resolved;
    }
    sort($verified);
    echo json_encode(array('backend' => $type, 'sockets' => $verified,
        'fingerprint' => hash('sha256', $master . "\0" . $dovecot . "\0" . json_encode($verified))), JSON_THROW_ON_ERROR);
} catch (Throwable $error) {
    fwrite(STDERR, 'Source submission inspection: ' . $error->getMessage() . "\n"); exit(1);
}
