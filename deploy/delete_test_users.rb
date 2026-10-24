# delete_test_users.rb - remove the auto-* test users created by the old
# per-caller bridge code (superseded: phone tickets now file under one
# shared "phone-customer"). Lists the users, then asks Y/N: press Y to
# delete, N to cancel. No typed confirmation word.
# Run WITHOUT -T so you can type the answer:
#   docker compose exec zammad-railsserver bundle exec rails runner /tmp/delete_test_users.rb
# Safety: only login LIKE 'auto-%'; skips any user who still has tickets
# and any admin account. Anything other than Y cancels. Console output is
# English-only on purpose.
users = User.where("login LIKE 'auto-%'").order(:id).to_a
if users.empty?
  puts 'No auto-* test users found.'
  exit 0
end
deletable = []
users.each do |u|
  if Ticket.where(customer_id: u.id).exists?
    puts "skip (has tickets): #{u.login} (id=#{u.id})"
  elsif u.roles.any? { |r| r.permissions.any? { |p| p.name == 'admin' } }
    puts "skip (admin): #{u.login} (id=#{u.id})"
  else
    deletable << u
    puts "will delete: #{u.login} (id=#{u.id}) #{u.firstname} #{u.lastname}"
  end
end
if deletable.empty?
  puts 'Nothing deletable.'
  exit 0
end
print "Delete these #{deletable.size} test users? Press Y to confirm, N to cancel: "
answer = STDIN.gets.to_s.strip.upcase
if answer == 'Y'
  deletable.each(&:destroy)
  puts "DELETED #{deletable.size} test users"
else
  puts 'Cancelled. Nothing deleted.'
end
