// This should NOT match printf rule since it's js not cpp
function test() {
  let printf = function() {};
  printf("test");
}
