<?php
function handoffWrite($file, $text, $hashMap = false) {
    fileText($file);
    $temporary = tempnam(dirname($file), '.migration-handoff-');
    chmod($temporary, 0600);
    if (file_put_contents($temporary, $text) !== strlen($text)) throw new RuntimeException('Incomplete write: ' . $file);
    if ($hashMap) {
        fileText($file . '.db');
        command(array('postmap', 'hash:' . $temporary));
        chmod($temporary . '.db', 0644);
        rename($temporary . '.db', $file . '.db');
    }
    chmod($temporary, strpos($file, '/etc/postfix/') === 0 ? 0644 : 0600);
    rename($temporary, $file);
}
function handoffTable($text) {
    $result = array();
    foreach (explode("\n", $text) as $line) {
        if (trim($line) === '' || preg_match('/^\s*#/', $line)) continue;
        if (!preg_match('/^(\S+)\s+(.+)$/', $line, $match) || isset($result[$match[1]])) {
            throw new RuntimeException('Unsupported or duplicate map entry: ' . $line);
        }
        $result[$match[1]] = trim($match[2]);
    }
    return $result;
}
function handoffMapText($entries) {
    ksort($entries);
    $text = "# Managed Mailcow migration routes\n";
    foreach ($entries as $key => $value) $text .= $key . "\t" . $value . "\n";
    return $text;
}
function handoffDomain($address) {
    return strtolower(substr(strrchr($address, '@') ?: '@' . $address, 1));
}
function handoffIdentity($entry) {
    unset($entry['route']);
    return $entry;
}
function handoffAliases($registry, $delimiter) {
    $text = "# Managed Mailcow migration recipient identities\n";
    ksort($registry);
    foreach ($registry as $domain => $entry) {
        foreach ($entry['recipients'] as $address) {
            $pattern = preg_quote($address, '/');
            if ($delimiter !== '') {
                $parts = explode('@', $address);
                $pattern = preg_quote($parts[0], '/') . '(' . preg_quote($delimiter, '/') . '[^@]*)?@' . preg_quote($domain, '/');
            }
            $text .= '/^(' . $pattern . ')$/ $1' . "\n";
        }
        if ($entry['catchAll']) $text .= '/^([^@]+@' . preg_quote($domain, '/') . ')$/ $1' . "\n";
    }
    return $text;
}
function handoffSmtpReply($socket, $expected, $operation) {
    $last = '';
    for ($line = 0; $line < 100; $line++) {
        $last = fgets($socket, 4096);
        if ($last === false || !preg_match('/^(\d{3})([ -])/', $last, $match)) {
            throw new RuntimeException('SMTP ' . $operation . ': no complete reply from Mailcow');
        }
        if ($match[2] === ' ') {
            if (!in_array((int)$match[1], $expected, true)) {
                throw new RuntimeException('SMTP ' . $operation . ': ' . trim($last));
            }
            return;
        }
    }
    throw new RuntimeException('SMTP ' . $operation . ': excessive multiline reply');
}
function handoffSmtpCommand($socket, $command, $expected) {
    $wire = $command . "\r\n";
    if (fwrite($socket, $wire) !== strlen($wire)) throw new RuntimeException('Incomplete SMTP probe command');
    handoffSmtpReply($socket, $expected, $command);
}
function handoffProbe($request) {
    $ip = $request['ip'];
    if (!filter_var($ip, FILTER_VALIDATE_IP)) throw new RuntimeException('Invalid Mailcow IP');
    $host = strpos($ip, ':') === false ? $ip : '[' . $ip . ']';
    $socket = stream_socket_client('tcp://' . $host . ':25', $error, $message, 10);
    if (!$socket) throw new RuntimeException('Cannot reach Mailcow TCP 25: ' . $message);
    stream_set_timeout($socket, 10);
    try {
        handoffSmtpReply($socket, array(220), 'greeting');
        handoffSmtpCommand($socket, 'EHLO ' . command(array('postconf', '-h', 'myhostname')), array(250));
        $probes = array_fill_keys($request['recipients'], array(250, 251));
        foreach ($request['rejected'] as $address) $probes[$address] = array(550, 551, 553);
        $probes['migration-' . bin2hex(random_bytes(12)) . '@' . $request['domain']] =
            $request['catchAll'] ? array(250, 251) : array(550, 551, 553);
        foreach ($probes as $address => $codes) {
            handoffSmtpCommand($socket, 'MAIL FROM:<>', array(250));
            handoffSmtpCommand($socket, 'RCPT TO:<' . $address . '>', $codes);
            handoffSmtpCommand($socket, 'RSET', array(250));
        }
        handoffSmtpCommand($socket, 'QUIT', array(221));
    } finally { fclose($socket); }
}
function handoffVerifyAliases($request, $lookup) {
    foreach ($request['recipients'] as $address) {
        if (command(array('postmap', '-q', $address, $lookup)) !== $address) {
            throw new RuntimeException('Recipient identity route failed for ' . $address);
        }
    }
    $unknown = 'migration-' . bin2hex(random_bytes(12)) . '@' . $request['domain'];
    if (command(array('postmap', '-q', $unknown, $lookup), true) !== ($request['catchAll'] ? $unknown : '')) {
        throw new RuntimeException('Generated alias map does not preserve catch-all/unknown-recipient behavior for ' . $request['domain']);
    }
}
function handoffEnvelope($id, $envelopeOnly) {
    // -e requires Postfix 2.7+. The -v format labels text records so body text cannot be mistaken for envelope fields.
    $args = array('postcat', '-q', $envelopeOnly ? '-e' : '-v', $id);
    $process = proc_open('exec ' . implode(' ', array_map('escapeshellarg', $args)),
        array(0 => array('file', '/dev/null', 'r'), 1 => array('pipe', 'w'), 2 => STDERR), $pipes);
    if (!is_resource($process)) throw new RuntimeException('Cannot start postcat for queue ' . $id);
    $fields = array(); $size = 0; $stage = 0; $queuePath = null; $senderSeen = false; $problem = null;
    $stages = array('ENVELOPE RECORDS', 'MESSAGE CONTENTS', 'HEADER EXTRACTED', 'MESSAGE FILE END');
    try {
        while (($chunk = fgets($pipes[1], 65536)) !== false) {
            // Drain long lines without retaining their contents or treating continuation chunks as new records.
            $long = substr($chunk, -1) !== "\n";
            $line = $long ? '' : rtrim($chunk, "\r\n");
            while ($long && ($chunk = fgets($pipes[1], 65536)) !== false) {
                if (substr($chunk, -1) === "\n") break;
            }
            if ($long) {
                if ($stage !== 2) $problem = 'oversized or unterminated envelope record';
                continue;
            }
            if ($problem !== null) continue;
            if (preg_match('/^\*\*\* (ENVELOPE RECORDS|MESSAGE CONTENTS|HEADER EXTRACTED|MESSAGE FILE END) (.+) \*\*\*$/', $line, $marker)) {
                if (($stages[$stage] ?? null) !== $marker[1] || basename($marker[2]) !== $id ||
                    ($queuePath !== null && $queuePath !== $marker[2])) {
                    $problem = 'unexpected envelope section order or queue path';
                    continue;
                }
                $queuePath = $marker[2]; $stage++;
                continue;
            }
            if ($stage === 2) continue; // Includes message headers, body and attachments; never log these lines.
            if (!in_array($stage, array(1, 3), true)) {
                $problem = 'output outside the expected envelope sections';
                continue;
            }
            if (!preg_match('/^(sender|original_recipient|recipient|done_recipient): /', $line, $field)) continue;
            $size += strlen($line) + 1;
            if ($size > 8 * 1024 * 1024) { $problem = 'envelope fields exceed 8 MiB'; continue; }
            if ($field[1] === 'sender') $senderSeen = true;
            $fields[] = $line;
        }
        if (!feof($pipes[1])) $problem = 'could not finish reading postcat output';
    } finally {
        fclose($pipes[1]);
        $status = proc_close($process);
    }
    // stderr is inherited directly: warnings/failures stay visible, stdout can never leak message content on failure.
    if ($status !== 0) throw new RuntimeException('postcat for queue ' . $id . ' exited ' . $status . '; see diagnostics above');
    if ($problem !== null || $stage !== 4 || !$senderSeen) {
        throw new RuntimeException('Cannot inspect queue ' . $id . ': ' . ($problem ?? 'incomplete envelope output'));
    }
    return $fields;
}
function handoffQueueIds() {
    $listing = command(array('postqueue', '-p'));
    preg_match_all('/^([a-zA-Z0-9]+)([*!]?)\s+\d+\s+/m', $listing, $ids, PREG_SET_ORDER);
    if (!$ids && strpos($listing, 'Mail queue is empty') === false) {
        throw new RuntimeException('Cannot interpret the old Postfix queue listing');
    }
    return $ids;
}
function handoffQueueProblem($id, $detail, $recipients) {
    return new RuntimeException('Queue ' . $id . ': ' . $detail .
        "\nPending recipients: " . implode(', ', array_unique($recipients)) .
        "\nReconcile this message, or discard it on the OLD server with:\n  postsuper -d " . $id .
        "\nDeletion removes this message for ALL remaining recipients, including any outside this domain." .
        "\nAfter resolving it, rerun the same command. No message was deleted automatically.");
}
function handoffQueue($request, $bcc) {
    $ids = handoffQueueIds();
    $retry = array(); $shared = array();
    static $envelopeOnly = null;
    if ($ids && $envelopeOnly === null) {
        $version = command(array('postconf', '-h', 'mail_version'));
        if (!preg_match('/^(\d+)\.(\d+)(?:\D|$)/', $version, $parts)) {
            throw new RuntimeException('Cannot determine postcat compatibility from Postfix mail_version: ' . $version);
        }
        $envelopeOnly = (int)$parts[1] > 2 || ((int)$parts[1] === 2 && (int)$parts[2] >= 7);
        if (!$envelopeOnly) fwrite(STDERR, 'Postfix ' . $version .
            ": using postcat -v; only envelope fields are retained for inspection. Queued messages remain unchanged.\n");
    }
    foreach ($ids as $item) {
        $id = $item[1];
        $protected = isset($request['legacyQueues']) && !in_array($id, $request['legacyQueues'], true);
        try { $envelope = handoffEnvelope($id, $envelopeOnly); }
        catch (RuntimeException $error) {
            // A queue file can finish delivery between listing and inspection. Diagnostics remain on stderr.
            if (!preg_match('/^' . preg_quote($id, '/') . '[*!]?\s+\d+\s/m', command(array('postqueue', '-p')))) continue;
            throw $error;
        }
        $original = ''; $sender = ''; $recipients = array(); $expanded = array(); $missingOriginal = array();
        foreach ($envelope as $line) {
            if (strpos($line, 'sender: ') === 0) $sender = substr($line, 8);
            if (strpos($line, 'original_recipient: ') === 0) $original = substr($line, 20);
            if (preg_match('/^(recipient|done_recipient): (.*)$/', $line, $match)) {
                if ($match[1] === 'recipient') {
                    $recipients[] = $match[2];
                    if (trim($original) === '' && handoffDomain($match[2]) === $request['domain']) {
                        $missingOriginal[] = $match[2];
                    }
                }
                if (handoffDomain($original) === $request['domain'] && strtolower($original) !== strtolower($match[2])) {
                    $expanded[] = $original . ' -> ' . $match[2];
                }
                $original = '';
            }
        }
        $batch = array_filter($recipients, function ($address) use ($request) { return handoffDomain($address) === $request['domain']; });
        if ($expanded) throw handoffQueueProblem($id, 'Already contains old alias/vacation expansion: ' .
            implode(', ', $expanded) . '. Original recipients need reconciliation before forwarding.', $recipients);
        if (!$batch) continue;
        if ($missingOriginal && !$protected) {
            throw handoffQueueProblem($id, 'Pending recipients for ' . $request['domain'] .
                ' without stored original-recipient history: ' . implode(', ', array_unique($missingOriginal)) . ".\n" .
                'Cannot establish whether old aliases/vacation rules already expanded these recipients; forwarding could apply rules twice.' .
                "\nCurrent enable_original_recipient = " . $request['originalRecipientSetting'] .
                '. Changing it now does not repair this queued message.', $recipients);
        }
        if ($item[2] === '!') throw handoffQueueProblem($id, 'Administratively held; it will not be released automatically.', $recipients);
        foreach ($protected ? array() : $bcc as $kind => $maps) {
            $addresses = $kind === 'sender_bcc_maps' ? array($sender) : $batch;
            foreach ($addresses as $address) {
                if (handoffDomain($address) !== $request['domain']) continue;
                foreach ($maps as $map) {
                    $local = explode('@', $address)[0];
                    $base = $request['delimiter'] === '' ? $local : explode($request['delimiter'], $local)[0];
                    foreach (array($address, $base . '@' . $request['domain'], '@' . $request['domain']) as $key) {
                        if (isset($map['originalEntries'][$key])) {
                            throw handoffQueueProblem($id, 'May already contain a copy from old ' . $kind . ' for ' . $address .
                                '. Reconcile its BCC recipients before forwarding; no automatic requeue is safe.', $recipients);
                        }
                    }
                }
            }
        }
        if (count($batch) === count($recipients) && $item[2] !== '*') $retry[] = $id;
        else $shared[] = $id;
    }
    return array('retry' => $retry, 'shared' => $shared);
}
try {
    $request = json_decode(stream_get_contents(STDIN), true, 512, JSON_THROW_ON_ERROR);
    $isolation = $request['isolation'] ?? false;
    $hold = 'retry:4.3.2 Domain migration in progress';
    $domain = $request['domain'];
    if (!preg_match('/^[a-z0-9.-]+$/', $domain)) throw new RuntimeException('Invalid handoff domain');
    foreach (array_merge($request['recipients'], $request['rejected'], $request['keys']) as $value) {
        if (!preg_match('/^[a-zA-Z0-9_.+%@-]+$/', $value)) throw new RuntimeException('Unsupported handoff address: ' . $value);
    }
    $root = '/root/mailcow-in-progress';
    if (realpath($root) !== $root) throw new RuntimeException('Missing or unsafe old-server migration directory');
    $registryFile = $root . '/forwarding.json';
    $registryText = fileText($registryFile);
    $registry = $registryText === '' ? array() : json_decode($registryText, true, 512, JSON_THROW_ON_ERROR);
    $isolationFile = $root . '/recipient-isolation-' . $domain . '.json';
    $isolationText = fileText($isolationFile);
    $isolated = $isolationText === '' ? null : json_decode($isolationText, true, 512, JSON_THROW_ON_ERROR);
    if ($isolated !== null && ($isolated['identity'] ?? null) !== handoffIdentity($request['entry'])) {
        throw new RuntimeException('Recipient isolation record differs for ' . $domain);
    }
    if (isset($registry[$domain]) && $registry[$domain] !== $request['entry'] &&
        ($registry[$domain]['route'] !== $hold || handoffIdentity($registry[$domain]) !== handoffIdentity($request['entry']))) {
        throw new RuntimeException('Existing forwarding record differs for ' . $domain . '; destination/plan cannot be changed implicitly');
    }
    $next = $registry; $next[$domain] = $request['entry'];
    $config = array();
    foreach (array('transport_maps', 'virtual_alias_maps', 'sender_bcc_maps', 'recipient_bcc_maps', 'always_bcc',
        'recipient_delimiter', 'enable_original_recipient', 'virtual_alias_domains') as $key) {
        $config[$key] = command(array('postconf', '-h', $key));
    }
    if ($config['always_bcc'] !== '') throw new RuntimeException('always_bcc is configured globally; a per-domain adapter is required');
    // The live setting cannot prove what historical queue files contain. Check actual pending recipients instead.
    $request['originalRecipientSetting'] = $config['enable_original_recipient'];
    $delimiter = $config['recipient_delimiter'];
    if ($delimiter !== '' && $delimiter !== '+') throw new RuntimeException('Only empty or + recipient_delimiter is supported for forwarding');
    $request['delimiter'] = $delimiter;
    $master = fileText('/etc/postfix/master.cf');
    if (preg_match('/-o\s+(transport_maps|virtual_alias_maps|sender_bcc_maps|recipient_bcc_maps|always_bcc)\s*=/',
        preg_replace('/^\s*#.*$/m', '', $master), $override)) {
        throw new RuntimeException('master.cf overrides ' . $override[1] . '; automatic handoff would not cover every ingress service');
    }
    if (!in_array('regexp', explode("\n", command(array('postconf', '-m'))), true)) throw new RuntimeException('Postfix regexp maps are unavailable');
    $map = '/etc/postfix/mailcow-migration-transport';
    $aliasFile = '/etc/postfix/mailcow-migration-aliases';
    $aliasLookup = 'regexp:' . $aliasFile;
    $chain = preg_split('/[\s,]+/', $config['transport_maps'], -1, PREG_SPLIT_NO_EMPTY);
    $preflight = $isolation && $request['action'] === 'inspect';
    if (($chain[0] ?? '') !== 'hash:' . $map && (!$preflight || in_array('hash:' . $map, $chain, true))) {
        throw new RuntimeException('Migration transport map is not first in transport_maps');
    }
    $routes = handoffTable(fileText($map));
    if ((!$preflight || file_exists($map)) &&
        handoffMapText(handoffTable(command(array('postmap', '-s', 'hash:' . $map)))) !== handoffMapText($routes)) {
        throw new RuntimeException('Migration transport text and compiled map disagree: ' . $map);
    }
    foreach ($request['keys'] as $key) {
        if ($preflight && !isset($routes[$key])) continue;
        if (!isset($routes[$key]) || !in_array($routes[$key], array($request['entry']['route'], $hold), true)) {
            throw new RuntimeException('Missing or conflicting migration transport for ' . $key);
        }
    }
    $aliases = preg_split('/[\s,]+/', $config['virtual_alias_maps'], -1, PREG_SPLIT_NO_EMPTY);
    if (in_array($aliasLookup, $aliases, true) && $aliases[0] !== $aliasLookup) throw new RuntimeException('Migration alias map is not first');
    $oldAliasText = handoffAliases($registry, $delimiter);
    $newAliasText = handoffAliases($next, $delimiter);
    $aliasText = fileText($aliasFile);
    if ($aliasText !== '' && $aliasText !== $oldAliasText && $aliasText !== $newAliasText) {
        throw new RuntimeException('Managed alias map changed outside the migration: ' . $aliasFile);
    }
    $bcc = array(); $files = array($map => fileText($map), $aliasFile => $aliasText);
    $newBcc = array();
    foreach (array('sender_bcc_maps', 'recipient_bcc_maps') as $kind) {
        $bcc[$kind] = array(); $lookups = array();
        foreach (preg_split('/[\s,]+/', $config[$kind], -1, PREG_SPLIT_NO_EMPTY) as $lookup) {
            if (!preg_match('#^hash:(/etc/postfix/[a-zA-Z0-9_./-]+)$#', $lookup, $match)) {
                throw new RuntimeException($kind . ' uses unsupported map ' . $lookup . '; no SQL or shared map will be guessed');
            }
            $original = preg_replace('/\.mailcow-migration$/', '', $match[1]);
            fileText($original); fileText($original . '.db');
            $entries = handoffTable(command(array('postmap', '-s', 'hash:' . $original)));
            $filtered = array_filter($entries, function ($key) use ($next) { return !isset($next[handoffDomain($key)]); }, ARRAY_FILTER_USE_KEY);
            $oldFiltered = array_filter($entries, function ($key) use ($registry) {
                return !isset($registry[handoffDomain($key)]);
            }, ARRAY_FILTER_USE_KEY);
            $copy = $original . '.mailcow-migration';
            $current = fileText($copy);
            if ($current !== '' && $current !== handoffMapText($oldFiltered) && $current !== handoffMapText($filtered)) {
                throw new RuntimeException('Managed BCC map differs from its source/registry: ' . $copy);
            }
            $files[$copy] = $current;
            $files[$original] = handoffMapText($entries);
            $bcc[$kind][] = array('file' => $copy, 'original' => $original, 'entries' => $filtered, 'originalEntries' => $entries);
            $lookups[] = 'hash:' . $copy;
        }
        $newBcc[$kind] = implode(', ', $lookups);
    }
    $fingerprint = hash('sha256', json_encode(array($config, $master, $files, $registryText, $isolationText), JSON_THROW_ON_ERROR));
    $mapped = ($aliases[0] ?? '') === $aliasLookup && $aliasText === $newAliasText;
    foreach ($newBcc as $kind => $value) {
        $mapped = $mapped && $config[$kind] === $value;
        foreach ($bcc[$kind] as $item) {
            $mapped = $mapped && fileText($item['file']) === handoffMapText($item['entries']) &&
                handoffMapText(handoffTable(command(array('postmap', '-s', 'hash:' . $item['file'])))) === handoffMapText($item['entries']);
        }
    }
    if ($isolated !== null) {
        if (!$mapped || !isset($isolated['legacyQueues']) || !is_array($isolated['legacyQueues'])) {
            throw new RuntimeException('Recipient isolation changed for ' . $domain . '; queued mail needs reconciliation');
        }
        $request['legacyQueues'] = $isolated['legacyQueues'];
    }
    $complete = $mapped && ($registry[$domain] ?? null) === $request['entry'] && (!$isolation || $isolated !== null);
    foreach ($request['keys'] as $key) {
        $complete = $complete && ($routes[$key] ?? null) === $request['entry']['route'] &&
            command(array('postmap', '-q', $key, 'hash:' . $map)) === $request['entry']['route'];
    }
    $queue = $isolation ? null : handoffQueue($request, $bcc);
    if (!$isolation) handoffProbe($request);
    if ($request['action'] === 'check') {
        if (!$complete) throw new RuntimeException('Domain recipient isolation is incomplete');
        handoffVerifyAliases($request, $aliasLookup);
        handoffQueue($request, $bcc);
        echo '{}'; exit(0);
    }
    if ($request['action'] === 'inspect') {
        echo json_encode(array('fingerprint' => $fingerprint, 'queue' => $queue, 'routes' => $request['keys'],
            'complete' => $complete, 'originalRecipientSetting' => $config['enable_original_recipient'],
            'bccMaps' => array_column(array_merge(...array_values($bcc)), 'file')), JSON_THROW_ON_ERROR); exit(0);
    }
    if ($request['action'] !== 'apply' || $request['fingerprint'] !== $fingerprint) {
        throw new RuntimeException('Old-server handoff configuration changed after preview; rerun');
    }
    $backupDirectory = $root . '/delivery-backups';
    if (!is_dir($backupDirectory) || realpath($backupDirectory) !== $backupDirectory) throw new RuntimeException('Unsafe delivery backup directory');
    $lockFile = $backupDirectory . '/config.lock';
    if (is_link($lockFile)) throw new RuntimeException('Unsafe delivery lock');
    $lock = fopen($lockFile, 'c');
    if (!flock($lock, LOCK_EX | LOCK_NB)) throw new RuntimeException('Another delivery operation is running');
    foreach ($config as $key => $value) {
        if (command(array('postconf', '-h', $key)) !== $value) throw new RuntimeException('Postfix changed before handoff: ' . $key);
    }
    if (fileText($registryFile) !== $registryText || fileText($isolationFile) !== $isolationText ||
        fileText($map) !== $files[$map] || fileText($aliasFile) !== $aliasText ||
        fileText('/etc/postfix/master.cf') !== $master) throw new RuntimeException('Source files changed before handoff');
    foreach ($bcc as $maps) foreach ($maps as $item) {
        if (fileText($item['file']) !== $files[$item['file']] ||
            handoffMapText(handoffTable(command(array('postmap', '-s', 'hash:' . $item['original'])))) !==
                handoffMapText($item['originalEntries'])) throw new RuntimeException('BCC map changed before handoff: ' . $item['original']);
    }
    if ($complete) {
        reloadPostfixAndDrain();
        handoffVerifyAliases($request, $aliasLookup);
        $queue = handoffQueue($request, $bcc);
        if (!$isolation) foreach ($queue['retry'] as $id) command(array('postqueue', '-i', $id));
        echo json_encode(array('backup' => null, 'queue' => $queue,
            'route' => $request['entry']['route'], 'alreadyComplete' => true), JSON_THROW_ON_ERROR); exit(0);
    }
    $backup = $backupDirectory . '/handoff-' . $fingerprint . '.json';
    $backupText = json_encode(array('config' => $config, 'files' => $files, 'registry' => $registryText,
        'recipientIsolation' => $isolationText), JSON_THROW_ON_ERROR);
    if (!file_exists($backup)) handoffWrite($backup, $backupText);
    if (fileText($backup) !== $backupText) throw new RuntimeException('Handoff backup does not match its original configuration');
    // Keep delivery deferred until mappings and the post-reload queue have been checked.
    foreach ($request['keys'] as $key) $routes[$key] = $hold;
    handoffWrite($map, handoffMapText($routes), true);
    reloadPostfixAndDrain();
    handoffWrite($aliasFile, $newAliasText);
    foreach ($bcc as $kind => $maps) foreach ($maps as $item) handoffWrite($item['file'], handoffMapText($item['entries']), true);
    if (!in_array($aliasLookup, $aliases, true)) {
        command(array('postconf', '-e', 'virtual_alias_maps = ' . $aliasLookup . ', ' . $config['virtual_alias_maps']));
    }
    foreach ($newBcc as $kind => $value) if ($value !== $config[$kind]) command(array('postconf', '-e', $kind . ' = ' . $value));
    reloadPostfixAndDrain();
    handoffVerifyAliases($request, $aliasLookup);
    if ($isolation && $isolated === null) {
        // Existing queues may contain recipients expanded before the old cleanup workers drained.
        $request['legacyQueues'] = array_column(handoffQueueIds(), 1);
        handoffWrite($isolationFile, json_encode(array('identity' => handoffIdentity($request['entry']),
            'legacyQueues' => $request['legacyQueues']), JSON_THROW_ON_ERROR));
        handoffWrite($registryFile, json_encode($next, JSON_THROW_ON_ERROR));
    }
    $queue = handoffQueue($request, $bcc);
    if (!$isolation) handoffProbe($request);
    handoffWrite($registryFile, json_encode($next, JSON_THROW_ON_ERROR));
    foreach ($request['keys'] as $key) $routes[$key] = $request['entry']['route'];
    handoffWrite($map, handoffMapText($routes), true);
    reloadPostfixAndDrain();
    foreach ($request['keys'] as $key) {
        if (command(array('postmap', '-q', $key, 'hash:' . $map)) !== $request['entry']['route']) {
            throw new RuntimeException('Final SMTP route verification failed for ' . $key);
        }
    }
    // No postqueue -f, postsuper -r or deletion: shared queues retain their normal scheduling.
    if (!$isolation) foreach ($queue['retry'] as $id) command(array('postqueue', '-i', $id));
    echo json_encode(array('backup' => $backup, 'queue' => $queue, 'route' => $request['entry']['route']), JSON_THROW_ON_ERROR);
} catch (Throwable $error) {
    fwrite(STDERR, 'Old-server handoff: ' . $error->getMessage() . "\n"); exit(1);
}
