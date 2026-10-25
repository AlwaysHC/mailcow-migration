<?php
try {
  require '/web/inc/vars.inc.php';
  require '/web/inc/functions.inc.php';
  $request = json_decode(stream_get_contents(STDIN), true, 512, JSON_THROW_ON_ERROR);
  $pdo = new PDO($database_type . ':unix_socket=' . $database_sock . ';dbname=' . $database_name, $database_user, $database_pass,
    array(PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION));
  $table = $request['kind'] === 'mailbox' ? 'mailbox' : 'admin';
  $role = $table === 'admin' ? ' AND superadmin = ' . ($request['kind'] === 'admin' ? '1' : '0') : '';
  $stmt = $pdo->prepare('SELECT password FROM ' . $table . ' WHERE username = ?' . $role);
  $verified = true;
  foreach ($request['users'] as $user) {
    $stmt->execute(array($user));
    $hashes = $stmt->fetchAll(PDO::FETCH_COLUMN);
    $expected = $request['password'];
    $verified = $verified && count($hashes) === 1 &&
      (hash_equals($hashes[0], $expected) || ($expected[0] !== '{' && verify_hash($hashes[0], $expected)));
  }
  echo json_encode($verified);
} catch (Throwable $error) { fwrite(STDERR, 'Password reconciliation: ' . $error->getMessage() . "\n"); exit(1); }
