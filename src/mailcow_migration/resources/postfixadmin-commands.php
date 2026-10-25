<?php
set_error_handler(function ($severity, $message, $file, $line) {
    throw new ErrorException($message, 0, $severity, $file, $line);
});
function command($args, $allowAbsent = false) {
    $process = proc_open(implode(' ', array_map('escapeshellarg', $args)),
        array(0 => array('file', '/dev/null', 'r'), 1 => array('pipe', 'w'), 2 => array('pipe', 'w')), $pipes);
    if (!is_resource($process)) throw new RuntimeException('Cannot start ' . $args[0]);
    $out = stream_get_contents($pipes[1]); fclose($pipes[1]);
    $err = stream_get_contents($pipes[2]); fclose($pipes[2]);
    $status = proc_close($process);
    if ($err !== '') fwrite(STDERR, $err);
    if ($allowAbsent && $status === 1 && $out === '' && $err === '') return '';
    if ($status !== 0) throw new RuntimeException(implode(' ', $args) . ' exited ' . $status . ': ' . trim($out));
    return trim($out);
}
function fileText($file) {
    if (is_link($file) || (file_exists($file) && (!is_file($file) || realpath($file) !== $file))) {
        throw new RuntimeException('Unsafe file: ' . $file);
    }
    return file_exists($file) ? file_get_contents($file) : '';
}
function processes() {
    $result = array();
    foreach (glob('/proc/[0-9]*/comm') as $file) {
        if (!file_exists($file)) continue;
        try { $name = trim(file_get_contents($file)); }
        catch (ErrorException $error) { if (!file_exists(dirname($file))) continue; throw $error; }
        if (in_array($name, array('qmgr', 'cleanup', 'pipe', 'local', 'virtual', 'deliver', 'dovecot-lda'), true)) {
            $result[] = basename(dirname($file));
        }
    }
    return $result;
}
function reloadPostfixAndDrain() {
    $workers = processes();
    command(array('postfix', 'check'));
    command(array('postfix', 'reload'));
    for ($attempt = 0; $attempt < 40; $attempt++) {
        $remaining = array_values(array_intersect($workers, processes()));
        if (!$remaining) return;
        usleep(250000);
    }
    throw new RuntimeException('Old Postfix workers have not drained; rerun. PIDs: ' . implode(', ', $remaining));
}
