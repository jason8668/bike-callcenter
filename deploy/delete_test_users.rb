# delete_test_users.rb - remove the auto-* test users created by the old
# per-caller bridge code (superseded: phone tickets now file under one
# shared "phone-customer"). Two steps, no typed confirmation word:
#   1. preview: bundle exec rails runner /tmp/delete_test_users.rb
#   2. delete:  CONFIRM=1 bundle exec rails runner /tmp/delete_test_users.rb
#      (via: docker compose exec -e CONFIRM=1 -T zammad-railsserver ...)
# Safety: only login LIKE 'auto-%'; skips any user who still has tickets
# and any admin account. Console output is English-only on purpose.
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
if ENV['CONFIRM'] == '1'
  deletable.each(&:destroy)
  puts "DELETED #{deletable.size} test users"
else
  puts 'Preview only. Re-run with CONFIRM=1 to actually delete.'
end
