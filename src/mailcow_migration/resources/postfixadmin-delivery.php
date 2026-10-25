<?php
try {
    $request = json_decode(stream_get_contents(STDIN), true, 512, JSON_THROW_ON_ERROR);
    $map = '/etc/postfix/mailcow-migration-transport';
    $lookup = 'hash:' . $map;
    $hold = 'retry:4.3.2 Domain migration in progress';
    $chain = command(array('postconf', '-h', 'transport_maps'));
    $text = fileText($map);
    $entries = array();
    foreach (explode("\n", $text) as $line) {
        if (trim($line) === '' || preg_match('/^\s*#/', $line)) continue;
        if (!preg_match('/^(\S+)\s+(.+)$/', trim($line), $match) || isset($entries[$match[1]])) {
            throw new RuntimeException('Invalid or duplicate migration transport entry');
        }
        $entries[$match[1]] = $match[2];
    }
    $master = fileText('/etc/postfix/master.cf');
    $master = preg_replace('/^\s*#.*$/m', '', $master);
    if (!preg_match('/^retry\s+unix\s+[^\n]*\s+error\s*$/m', $master)) {
        throw new RuntimeException('Postfix retry/error transport is absent from /etc/postfix/master.cf');
    }
    if (preg_match('/-o\s+transport_maps\s*=/', $master)) {
        throw new RuntimeException('A master.cf service overrides transport_maps; its delivery path needs an isolation adapter');
    }
    $maps = preg_split('/[\s,]+/', $chain, -1, PREG_SPLIT_NO_EMPTY);
    if (in_array($lookup, $maps, true) && $maps[0] !== $lookup) {
        throw new RuntimeException('Migration transport map is not first in transport_maps; refusing to reorder existing routes');
    }
    $fingerprint = hash('sha256', $chain . "\0" . $text);
    $blockers = array();
    foreach (array_merge(glob('/etc/cron.d/*'), glob('/var/spool/cron/*'), array('/etc/crontab')) as $file) {
        if (!is_file($file)) continue;
        foreach (explode("\n", file_get_contents($file)) as $number => $line) {
            if (trim($line) === '' || preg_match('/^\s*#/', $line)) continue;
            $batchReference = null;
            foreach (array_merge($request['roots'], $request['keys']) as $reference) {
                if (preg_match('/(?<![a-zA-Z0-9_.+%@\/-])' . preg_quote($reference, '/') . '(?![a-zA-Z0-9_.+%@-])/', $line)) {
                    $batchReference = $reference; break;
                }
            }
            if ($batchReference !== null && preg_match('/\b(fetchmail|getmail|imapsync|offlineimap|mbsync|dsync)\b/i', $line, $writer)) {
                $blockers[] = 'Scheduled ' . $writer[1] . ' job explicitly references this batch (' . $batchReference . '): ' .
                    $file . ':' . ($number + 1) . '. Pause this job during the final copy, then rerun. No cron entry was changed.';
            }
        }
    }
    if ($request['action'] === 'inspect') {
        echo json_encode(array('fingerprint' => $fingerprint, 'chain' => $chain, 'entries' => (object)$entries,
            'blockers' => $blockers), JSON_THROW_ON_ERROR); exit(0);
    }
    if ($request['action'] === 'check') {
        if ($maps[0] !== $lookup) throw new RuntimeException('Source delivery hold is not the first transport map');
        foreach ($request['keys'] as $key) {
            if (($entries[$key] ?? '') !== $hold || command(array('postmap', '-q', $key, $lookup)) !== $hold) {
                throw new RuntimeException('Source delivery hold missing or changed for ' . $key);
            }
        }
        foreach (glob('/proc/[0-9]*/fd') as $directory) {
            foreach (glob($directory . '/*') as $fd) {
                if (!is_link($fd)) continue;
                try { $target = readlink($fd); }
                catch (ErrorException $error) { if (!is_link($fd)) continue; throw $error; }
                foreach ($request['roots'] as $root) {
                    if (strpos($target, $root . '/') !== 0) continue;
                    $infoFile = dirname($directory) . '/fdinfo/' . basename($fd);
                    if (!file_exists($infoFile)) continue;
                    try { $info = file_get_contents($infoFile); }
                    catch (ErrorException $error) { if (!file_exists($infoFile)) continue; throw $error; }
                    if (!preg_match('/^flags:\s+([0-7]+)/m', $info, $flags)) {
                        throw new RuntimeException('Cannot inspect access mode for ' . $fd);
                    }
                    if ((octdec($flags[1]) & 3) !== 0) {
                        $blockers[] = 'Open writable mailbox file: PID ' . basename(dirname($directory)) . ', ' . $target;
                    }
                }
            }
        }
        if ($blockers) throw new RuntimeException(implode("\n", $blockers));
        echo '{}'; exit(0);
    }
    if ($request['action'] !== 'apply' || $request['fingerprint'] !== $fingerprint) {
        throw new RuntimeException('Source delivery configuration changed after preview; rerun');
    }
    if ($blockers) throw new RuntimeException(implode("\n", $blockers));
    foreach ($request['keys'] as $key) {
        if (!preg_match('/^[a-zA-Z0-9_.+%@-]+$/', $key)) throw new RuntimeException('Unsupported transport key: ' . $key);
        if (isset($entries[$key]) && $entries[$key] !== $hold) throw new RuntimeException('Conflicting migration route for ' . $key);
        $entries[$key] = $hold;
    }
    $directory = '/root/mailcow-in-progress/delivery-backups';
    if (!file_exists($directory)) mkdir($directory, 0700, true);
    if (realpath($directory) !== $directory) throw new RuntimeException('Unsafe backup directory');
    chmod($directory, 0700);
    $lockFile = $directory . '/config.lock';
    if (is_link($lockFile)) throw new RuntimeException('Unsafe lock path');
    $lock = fopen($lockFile, 'c');
    if (!flock($lock, LOCK_EX | LOCK_NB)) throw new RuntimeException('Another delivery configuration operation is running');
    if (hash('sha256', command(array('postconf', '-h', 'transport_maps')) . "\0" . fileText($map)) !== $fingerprint) {
        throw new RuntimeException('Source delivery configuration changed before writing');
    }
    $backup = $directory . '/' . $fingerprint . '.json';
    $backupData = json_encode(array('transport_maps' => $chain, 'map' => $text), JSON_THROW_ON_ERROR);
    if (!file_exists($backup)) {
        $handle = fopen($backup, 'x'); chmod($backup, 0600);
        if (fwrite($handle, $backupData) !== strlen($backupData)) throw new RuntimeException('Incomplete delivery backup');
        fclose($handle);
    }
    if (fileText($backup) !== $backupData) throw new RuntimeException('Delivery backup differs from the original');
    ksort($entries);
    $after = "# Managed per-domain migration delivery holds\n";
    foreach ($entries as $key => $value) $after .= $key . ' ' . $value . "\n";
    if ($after !== $text) {
        $temporary = tempnam('/etc/postfix', '.migration-transport-');
        chmod($temporary, 0644);
        if (file_put_contents($temporary, $after) !== strlen($after)) throw new RuntimeException('Incomplete transport map');
        command(array('postmap', 'hash:' . $temporary));
        rename($temporary . '.db', $map . '.db');
        rename($temporary, $map);
    } else command(array('postmap', $lookup));
    if (!in_array($lookup, $maps, true)) command(array('postconf', '-e', 'transport_maps = ' . $lookup . ', ' . $chain));
    // Drain workers that might have selected a local delivery route before the hold was installed.
    reloadPostfixAndDrain();
    echo json_encode(array('backup' => $backup), JSON_THROW_ON_ERROR);
} catch (Throwable $error) {
    fwrite(STDERR, 'Source delivery isolation: ' . $error->getMessage() . "\n"); exit(1);
}
