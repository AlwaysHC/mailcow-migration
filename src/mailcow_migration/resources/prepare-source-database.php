<?php
set_error_handler(function ($severity, $message, $file, $line) {
    throw new ErrorException($message, 0, $severity, $file, $line);
});
try {
    $request = json_decode(stream_get_contents(STDIN), true, 512, JSON_THROW_ON_ERROR);
    $file = $request['file'];
    if (!in_array($request['action'], array('inspect', 'create'), true) || !is_string($file) ||
        $file === '' || $file[0] !== '/' || is_link($file) || realpath(dirname($file)) !== dirname($file)) {
        throw new RuntimeException('Unsafe database option-file request');
    }
    if (file_exists($file)) {
        if (!is_file($file) || (fileperms($file) & 0077)) throw new RuntimeException('Database option file must be private (mode 600)');
        if (!preg_match('/^[ \t]*\[client\][ \t]*$/m', file_get_contents($file))) {
            throw new RuntimeException('Database option file must contain a client section; no contents were returned');
        }
        echo json_encode(array('exists' => true, 'created' => false), JSON_THROW_ON_ERROR); exit(0);
    }
    $source = '/etc/postfix/sql/mysql_virtual_domains_maps.cf';
    if (!is_file($source)) throw new RuntimeException('Cannot discover database credentials; provide a private MySQL option file');
    $config = array();
    foreach (explode("\n", file_get_contents($source)) as $line) {
        if (preg_match('/^\s*(user|password|hosts|dbname)\s*=\s*(.*?)\s*$/', $line, $match)) {
            if (isset($config[$match[1]])) throw new RuntimeException('Duplicate source database setting');
            $config[$match[1]] = $match[2];
        }
    }
    foreach (array('user', 'password', 'hosts', 'dbname') as $key) {
        if (!isset($config[$key])) throw new RuntimeException('Missing source database setting');
    }
    if ($config['dbname'] !== 'postfix' || !preg_match('/^[a-zA-Z0-9_.-]+$/', $config['hosts'])) {
        throw new RuntimeException('Automatic credential discovery requires postfix and a single database hostname');
    }
    $text = "[client]\n";
    foreach (array('user' => 'user', 'password' => 'password', 'host' => 'hosts') as $option => $key) {
        if (preg_match('/[\r\n\x00]/', $config[$key])) throw new RuntimeException('Unsupported database option value');
        $text .= $option . '="' . str_replace(array('\\', '"'), array('\\\\', '\\"'), $config[$key]) . "\"\n";
    }
    if ($request['action'] === 'inspect') {
        echo json_encode(array('exists' => false, 'created' => false), JSON_THROW_ON_ERROR); exit(0);
    }
    $temporary = tempnam(dirname($file), '.migration-db-');
    chmod($temporary, 0600);
    if (file_put_contents($temporary, $text) !== strlen($text)) throw new RuntimeException('Incomplete database option-file write');
    if (file_exists($file) || is_link($file)) throw new RuntimeException('Database option-file path changed during preparation');
    if (!link($temporary, $file)) throw new RuntimeException('Could not create the database option file without overwriting');
    unlink($temporary);
    echo json_encode(array('exists' => true, 'created' => true), JSON_THROW_ON_ERROR);
} catch (Throwable $error) {
    fwrite(STDERR, 'Source database preparation: ' . $error->getMessage() . "\n"); exit(1);
}
