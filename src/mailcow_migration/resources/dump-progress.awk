# mysqldump --skip-extended-insert emits one INSERT per row. SQL only goes to stdout.
function progress() {
    percent = total > 0 ? int(count * 100 / total) : 0
    # Only the shell can confirm that the whole pipeline succeeded.
    if (percent > 99) percent = 99
    if (percent == previous) return
    previous = percent
    bar = ""
    for (i = 0; i < 20; i++) bar = bar (i < int(percent / 5) ? "#" : "-")
    printf "SQL rows [%s] %d%% %d/%d\n", bar, percent, count, total > "/dev/stderr"
}
BEGIN { count = 0; previous = -1; progress() }
{
    print
    if ($0 ~ /^INSERT INTO `[^`]+` VALUES /) { count++; progress() }
}
END { print count > count_file }
