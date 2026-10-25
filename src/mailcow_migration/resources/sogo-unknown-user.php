<?php
$request = json_decode(stream_get_contents(STDIN), true, 512, JSON_THROW_ON_ERROR);
$cache = new Memcached();
foreach (explode(",", $request["host"]) as $host) {
    $server = parse_url("tcp://" . trim($host));
    if (!$server || empty($server["host"]) || array_diff(array_keys($server), ["scheme", "host", "port"])) {
        throw new RuntimeException("Unsupported SOGo Memcached host");
    }
    if (!$cache->addServer($server["host"], $server["port"] ?? 11211)) throw new RuntimeException($cache->getResultMessage());
}
$key = $request["username"] . "+attributes";
$value = $cache->get($key);
if (!in_array($cache->getResultCode(), [Memcached::RES_SUCCESS, Memcached::RES_NOTFOUND], true)) {
    throw new RuntimeException($cache->getResultMessage());
}
$unknown = is_string($value) && trim($value) === "null";
if ($unknown && !$cache->delete($key) && $cache->getResultCode() !== Memcached::RES_NOTFOUND) {
    throw new RuntimeException($cache->getResultMessage());
}
echo $unknown ? "cleared" : "unchanged";
