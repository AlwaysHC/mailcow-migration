<?php
set_error_handler(function ($severity, $message, $file, $line) {
    throw new ErrorException($message, 0, $severity, $file, $line);
});
try {
    $request = json_decode(stream_get_contents(STDIN), true, 512, JSON_THROW_ON_ERROR);
    $file = '/etc/dovecot/dovecot-mysql.conf';
    if (realpath($file) !== $file || !is_file($file) || stat($file)['nlink'] !== 1) {
        throw new RuntimeException('Dovecot SQL configuration must be a regular file without symlinks or hard links');
    }
    $before = file_get_contents($file);
    $hash = hash('sha256', $before);
    if (preg_match_all('/^[ \t]*password_query[ \t]*=[ \t]*([^\r\n]*)/m', $before, $matches, PREG_OFFSET_CAPTURE) !== 1) {
        throw new RuntimeException('Expected exactly one single-line password_query');
    }
    $query = rtrim($matches[1][0][0]);
    if ($request['action'] === 'read') {
        echo json_encode(array('query' => $query, 'hash' => $hash), JSON_THROW_ON_ERROR);
        exit(0);
    }
    if ($request['action'] !== 'write') throw new RuntimeException('Unknown configuration operation');
    $directory = '/root/mailcow-in-progress/auth-backups';
    foreach (array('/root/mailcow-in-progress', $directory) as $folder) {
        if (!file_exists($folder)) mkdir($folder, 0700);
        if (realpath($folder) !== $folder || !is_dir($folder)) throw new RuntimeException('Unsafe backup directory');
        chmod($folder, 0700);
    }
    $lockPath = $directory . '/config.lock';
    if (is_link($lockPath)) throw new RuntimeException('Unsafe configuration lock');
    $lock = fopen($lockPath, 'c');
    if (!flock($lock, LOCK_EX | LOCK_NB)) throw new RuntimeException('Another login configuration change is running');
    if ($hash !== $request['expectedHash'] || hash_file('sha256', $file) !== $request['expectedHash']) {
        throw new RuntimeException('Dovecot SQL configuration changed after preview; rerun to review it again');
    }
    if (!is_string($request['query']) || preg_match('/[\r\n\x00]/', $request['query'])) {
        throw new RuntimeException('Invalid replacement query');
    }
    $after = substr_replace($before, $request['query'], $matches[1][0][1], strlen($matches[1][0][0]));
    $backup = $directory . '/' . $hash . '.conf';
    if (is_link($backup)) throw new RuntimeException('Unsafe backup path');
    if (!file_exists($backup)) {
        $handle = fopen($backup, 'x');
        chmod($backup, 0600);
        if (fwrite($handle, $before) !== strlen($before)) throw new RuntimeException('Incomplete backup');
        fclose($handle);
    }
    if (hash_file('sha256', $backup) !== $hash) throw new RuntimeException('Backup verification failed');
    $temporary = tempnam(dirname($file), '.migration-auth-');
    passthru('cp --preserve=all -- ' . escapeshellarg($file) . ' ' . escapeshellarg($temporary), $status);
    if ($status !== 0) throw new RuntimeException('Cannot preserve Dovecot configuration ownership, permissions and attributes');
    if (file_put_contents($temporary, $after) !== strlen($after)) throw new RuntimeException('Incomplete replacement');
    if (hash_file('sha256', $file) !== $request['expectedHash']) throw new RuntimeException('Configuration changed before replacement');
    rename($temporary, $file);
    echo json_encode(array('backup' => $backup, 'hash' => hash('sha256', $after)), JSON_THROW_ON_ERROR);
} catch (Throwable $error) {
    fwrite(STDERR, 'Source login configuration: ' . $error->getMessage() . "\n");
    exit(1);
}
