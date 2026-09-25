const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const model = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../Filesystems.js'), 'utf8'), model);
test('local disk capacities deduplicate subvolumes and prefer root', () => {
  const data = model.parse(`Filesystem Type 1024-blocks Used Available Capacity Mounted on
/dev/nvme0n1p2 btrfs 2000 1000 1000 50% /home
/dev/nvme0n1p2 btrfs 2000 1000 1000 50% /
/dev/nvme0n1p1 vfat 500 100 400 20% /boot
tmpfs tmpfs 1000 1 999 1% /run
/dev/sdb1 ext4 1000 200 800 20% /media/My Disk`);
  assert.equal(data.length, 3);
  assert.equal(data[0].mount, '/');
  assert.equal(data[2].mount, '/media/My Disk');
  assert.equal(data[2].usedKb, 200);
});
test('empty, malformed and zero-capacity results are unavailable', () => {
  assert.equal(model.parse('').length, 0);
  assert.equal(model.parse('header\n/dev/sda ext4 0 0 0 - /mnt\nbad row').length, 0);
});
