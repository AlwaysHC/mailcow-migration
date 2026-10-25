<?php
// Snapshot reads are read-only. Explicit session actions affect only batch login sessions.
declare(strict_types=1);

$migrationPhase = 'starting the exporter';
$migrationFinished = false;
set_error_handler(static function (int $severity, string $message, string $file, int $line): bool {
    throw new ErrorException($message, 0, $severity, $file, $line);
});
register_shutdown_function(static function () use (&$migrationPhase, &$migrationFinished): void {
    if (!$migrationFinished) {
        fwrite(STDERR, "Roundcube export stopped unexpectedly while $migrationPhase.\n"
            . "Check the PHP error above and any exit/die statement in the configuration loaded at this stage.\n");
        exit(1);
    }
});

try {
    $migrationPhase = 'reading the export request';
    $request = json_decode(base64_decode($argv[1], true), true, 512, JSON_THROW_ON_ERROR);
    $root = realpath($request['directory']);
    if ($root === false || !is_file($root . '/config/config.inc.php')) {
        throw new RuntimeException('Roundcube config/config.inc.php was not found in the supplied directory');
    }
    define('INSTALL_PATH', $root . '/');
    // defaults.inc.php uses this constant even when the web application is not bootstrapped.
    define('RCUBE_INSTALL_PATH', INSTALL_PATH);
    define('RCMAIL_CONFIG_DIR', $root . '/config');
    define('RCUBE_CONFIG_DIR', RCMAIL_CONFIG_DIR . '/');
    $migrationPhase = 'reading the installed Roundcube version';
    $bootstrap = file_get_contents($root . '/program/include/iniset.php');
    if (!preg_match('/define\(\s*[\'"]RCMAIL_VERSION[\'"]\s*,\s*[\'"]([^\'"]+)/', $bootstrap, $match)) {
        throw new RuntimeException('Cannot identify the installed Roundcube version');
    }
    $version = $match[1];
    if (!preg_match('/^1\.6\.\d+$/', $version)) {
        throw new RuntimeException("Roundcube $version needs a reviewed exporter; this exporter supports 1.6.x");
    }
    define('RCMAIL_VERSION', $version);
    $config = [];
    $rcmail_config = [];
    $migrationPhase = 'loading ' . $root . '/config/defaults.inc.php';
    require $root . '/config/defaults.inc.php';
    $migrationPhase = 'loading ' . $root . '/config/config.inc.php';
    require $root . '/config/config.inc.php';
    if (($request['sourceKind'] ?? '') === 'plesk' && is_file($root . '/config/config.local.php')) {
        $migrationPhase = 'loading ' . $root . '/config/config.local.php';
        require_once $root . '/config/config.local.php';
    }
    $migrationPhase = 'validating Roundcube configuration';
    $config = array_merge($rcmail_config, $config);
    if (!empty($config['include_host_config'])) {
        throw new RuntimeException('Per-host Roundcube configuration is enabled; its effective settings need a reviewed export');
    }
    if (!empty($config['ldap_public']) || !empty($config['address_book_type']) && $config['address_book_type'] !== 'sql') {
        throw new RuntimeException('External or non-SQL address books are configured; their contacts need a separate export');
    }
    $unsupported = array_diff($config['plugins'] ?? [], ['archive', 'zipdownload', 'managesieve', 'emoticons', 'password', 'markasjunk']);
    if ($unsupported) {
        throw new RuntimeException('Unmapped Roundcube plugin data: ' . implode(', ', $unsupported));
    }
    $dsn = parse_url($config['db_dsnw'] ?? '');
    if (!$dsn || !in_array($dsn['scheme'] ?? '', ['mysql', 'mysqli'], true) || !empty($dsn['query'])) {
        throw new RuntimeException('Expected a MySQL/Mysqli DSN without extra connection options');
    }
    $database = rawurldecode(ltrim($dsn['path'] ?? '', '/'));
    $host = rawurldecode($dsn['host'] ?? 'localhost');
    if (!preg_match('/^[a-zA-Z0-9_]+$/', $database) || !preg_match('/^[a-zA-Z0-9_.:-]+$/', $host)) {
        throw new RuntimeException('Unsupported Roundcube database name/host');
    }
    $prefix = $config['db_prefix'] ?? '';
    if (!preg_match('/^[a-zA-Z0-9_]*$/', $prefix)) {
        throw new RuntimeException('Unsupported Roundcube table prefix');
    }
    $migrationPhase = 'connecting to the Roundcube database';
    $connection = new PDO(
        'mysql:host=' . $host . ';port=' . ($dsn['port'] ?? 3306) . ';dbname=' . $database . ';charset=utf8mb4',
        rawurldecode($dsn['user'] ?? ''),
        rawurldecode($dsn['pass'] ?? ''),
        [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION, PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC]
    );
    if (in_array($request['action'] ?? '', ['inspect-sessions', 'revoke-sessions'], true)) {
        $migrationPhase = 'isolating Roundcube login sessions';
        if (($config['session_storage'] ?? 'db') !== 'db') {
            throw new RuntimeException('Roundcube session_storage is not db; automatic session isolation needs its storage adapter');
        }
        require_once $root . '/program/lib/Roundcube/rcube_session.php';
        $connection->beginTransaction();
        $accounts = $connection->query("SELECT user_id, username FROM `{$prefix}users`")->fetchAll();
        $owners = [];
        foreach ($accounts as $account) {
            $login = strtolower($account['username']);
            $parts = explode('@', $login);
            if (count($parts) === 2 && in_array($parts[1], $request['domains'], true)) {
                if (!in_array($parts[0] . '@' . $request['domain'], $request['users'], true)) {
                    continue;
                }
                $owners[(string) $account['user_id']] = $login;
            }
        }
        $counts = array_fill_keys(array_values($owners), 0);
        $delete = $connection->prepare("DELETE FROM `{$prefix}session` WHERE sess_id=? AND BINARY vars=?");
        $sessions = $connection->query("SELECT sess_id, vars FROM `{$prefix}session`")->fetchAll();
        foreach ($sessions as $session) {
            $raw = base64_decode($session['vars'], true);
            if ($raw === false) throw new RuntimeException('Invalid Roundcube session encoding; cannot establish ownership');
            $data = rcube_session::unserialize($raw);
            if (!is_array($data)) throw new RuntimeException('Invalid Roundcube session data; cannot establish ownership');
            $owner = $owners[(string) ($data['user_id'] ?? '')] ?? null;
            if ($owner === null) continue;
            if (strtolower($data['username'] ?? '') !== $owner) {
                throw new RuntimeException("Roundcube session ownership mismatch for $owner");
            }
            $counts[$owner]++;
            if ($request['action'] === 'revoke-sessions') {
                $delete->execute([$session['sess_id'], $session['vars']]);
                if ($delete->rowCount() !== 1) throw new RuntimeException("Roundcube session changed during isolation for $owner; retry");
            }
        }
        $connection->commit();
        echo json_encode(['action' => $request['action'], 'sessions' => (object) $counts], JSON_THROW_ON_ERROR);
        $migrationFinished = true;
        exit(0);
    }
    $migrationPhase = 'starting the consistent Roundcube snapshot';
    // Inventory persistent user data in one consistent snapshot; SELECTs never change the source.
    $connection->exec('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ');
    $connection->exec('START TRANSACTION WITH CONSISTENT SNAPSHOT');
    $migrationPhase = 'reading the Roundcube database inventory';
    $query = static function (string $sql, array $arguments = []) use ($connection): array {
        $statement = $connection->prepare($sql);
        $statement->execute($arguments);
        return $statement->fetchAll();
    };
    $persistent = ['users', 'contacts', 'contactgroups', 'contactgroupmembers', 'identities', 'responses',
        'dictionary', 'searches', 'collected_addresses', 'filestore'];
    $tables = array_map(static function (string $name) use ($prefix): string { return $prefix . $name; }, $persistent);
    $engines = $query('SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = ?', [$database]);
    foreach ($tables as $table) {
        $found = array_values(array_filter($engines, static function (array $row) use ($table): bool {
            return $row['TABLE_NAME'] === $table;
        }));
        if (count($found) !== 1 || strtoupper($found[0]['ENGINE']) !== 'INNODB') {
            throw new RuntimeException("Table $table is missing or not InnoDB; a consistent user-data snapshot cannot be established");
        }
    }
    $knownTables = array_merge($persistent, ['session', 'cache', 'cache_shared', 'cache_index', 'cache_thread', 'cache_messages', 'system']);
    $unknownTables = [];
    foreach ($engines as $table) {
        $name = $table['TABLE_NAME'];
        if (substr($name, 0, strlen($prefix)) === $prefix && !in_array(substr($name, strlen($prefix)), $knownTables, true)) {
            $unknownTables[] = $name;
        }
    }
    $accounts = $query("SELECT u.user_id, u.username, u.mail_host, u.last_login, u.language, u.preferences,
        (SELECT COUNT(*) FROM `{$prefix}contacts` c WHERE c.user_id=u.user_id AND c.del=0) AS contacts,
        (SELECT COUNT(*) FROM `{$prefix}contactgroups` g WHERE g.user_id=u.user_id AND g.del=0) AS groups
        FROM `{$prefix}users` u ORDER BY u.last_login DESC, u.user_id DESC");
    $users = [];
    $selectedUsers = [];
    $ignoredUsers = [];
    $defaults = array_intersect_key($config, array_flip([
        'language', 'timezone', 'htmleditor', 'reply_mode', 'sig_below', 'show_sig', 'sig_separator',
        'draft_autosave', 'compose_extwin', 'forward_attachment', 'drafts_mbox', 'sent_mbox', 'trash_mbox', 'junk_mbox',
        'mdn_default', 'dsn_default', 'reply_same_folder', 'mail_read_time', 'default_list_mode',
        'message_threading', 'message_extwin', 'prefer_html'
    ]));
    $sharedDictionary = $query("SELECT * FROM `{$prefix}dictionary` WHERE user_id IS NULL ORDER BY language");
    foreach ($accounts as $account) {
        $username = strtolower($account['username']);
        $parts = explode('@', $username);
        if (count($parts) !== 2) {
            throw new RuntimeException("Roundcube login '$username' is not a full email address; its user-data ownership needs mapping");
        }
        if (!in_array($parts[1], $request['domains'], true)) {
            continue;
        }
        $migrationPhase = 'reading Roundcube data for ' . $username;
        $target = $parts[0] . '@' . $request['domain'];
        $missingMailbox = !in_array($target, $request['users'], true);
        $id = $account['user_id'];
        if ($missingMailbox || isset($selectedUsers[$target])) {
            $ignoredUsers[] = ['id' => (string) $id, 'login' => $username, 'target' => $target,
                'mailHost' => $account['mail_host'], 'lastLogin' => $account['last_login']]
                + ($missingMailbox
                    ? ['reason' => 'missing-mailbox', 'contacts' => (int) $account['contacts'], 'groups' => (int) $account['groups']]
                    : ['selectedId' => $selectedUsers[$target]]);
            continue;
        }
        $selectedUsers[$target] = (string) $id;
        $contacts = $query("SELECT contact_id, name, firstname, surname, email, vcard
            FROM `{$prefix}contacts` WHERE user_id=? AND del=0 ORDER BY contact_id", [$id]);
        $groups = $query("SELECT contactgroup_id, name FROM `{$prefix}contactgroups`
            WHERE user_id=? AND del=0 ORDER BY contactgroup_id", [$id]);
        $members = $query("SELECT m.contactgroup_id, m.contact_id FROM `{$prefix}contactgroupmembers` m
            JOIN `{$prefix}contactgroups` g ON g.contactgroup_id=m.contactgroup_id
            JOIN `{$prefix}contacts` c ON c.contact_id=m.contact_id
            WHERE g.user_id=? AND c.user_id=? AND g.del=0 AND c.del=0 ORDER BY m.contactgroup_id, m.contact_id", [$id, $id]);
        $identities = $query("SELECT * FROM `{$prefix}identities` WHERE user_id=? AND del=0 ORDER BY identity_id", [$id]);
        $responses = $query("SELECT * FROM `{$prefix}responses` WHERE user_id=? ORDER BY response_id", [$id]);
        $dictionary = $query("SELECT * FROM `{$prefix}dictionary` WHERE user_id=? ORDER BY language", [$id]);
        $searches = $query("SELECT * FROM `{$prefix}searches` WHERE user_id=? ORDER BY search_id", [$id]);
        $collectedAddresses = $query("SELECT * FROM `{$prefix}collected_addresses`
            WHERE user_id=? ORDER BY address_id", [$id]);
        // File content is not imported yet. Count and fingerprint each file so it cannot be silently omitted or changed.
        $files = $query("SELECT file_id, context, filename, mtime, OCTET_LENGTH(data) AS bytes, SHA2(data, 256) AS digest
            FROM `{$prefix}filestore` WHERE user_id=? ORDER BY file_id", [$id]);
        $preferences = $account['preferences'] ? unserialize($account['preferences'], ['allowed_classes' => false]) : [];
        if (!is_array($preferences)) {
            throw new RuntimeException("Roundcube preferences for $username are not a supported serialized array");
        }
        $users[] = ['id' => (string) $id, 'login' => $username, 'target' => $target,
            'mailHost' => $account['mail_host'], 'lastLogin' => $account['last_login'],
            'contacts' => $contacts, 'groups' => $groups, 'members' => $members,
            'language' => $account['language'], 'preferences' => $preferences,
            'preferencesSerialized' => base64_encode($account['preferences'] ?? ''),
            'identities' => $identities, 'responses' => $responses, 'dictionary' => $dictionary, 'searches' => $searches,
            'collectedAddresses' => $collectedAddresses, 'files' => $files];
    }
    $migrationPhase = 'closing the consistent Roundcube snapshot';
    // Match the SQL START TRANSACTION; older PDO drivers do not track SQL-started transactions.
    $connection->exec('COMMIT');
    $migrationPhase = 'encoding the complete Roundcube snapshot';
    echo json_encode(['format' => 4, 'version' => $version, 'database' => $database, 'directory' => $root,
        'unknownTables' => $unknownTables, 'defaults' => $defaults, 'sharedDictionary' => $sharedDictionary,
        'users' => $users, 'ignoredUsers' => $ignoredUsers], JSON_THROW_ON_ERROR);
    $migrationFinished = true;
} catch (Throwable $error) {
    $migrationFinished = true;
    fwrite(STDERR, "Roundcube export stopped while $migrationPhase:\n" . $error->getMessage()
        . "\nFile: " . $error->getFile() . ':' . $error->getLine() . "\n");
    exit(1);
}
