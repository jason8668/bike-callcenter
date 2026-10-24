# simplify_roles_groups.rb - slim the system down to the two roles this
# project needs (system admin + agent) and a single ticket group.
#
# What it does, after a Y/N confirmation (press Y to apply, N to cancel):
#   - deactivates every group except 客服组 (tickets all live there now;
#     dispatch is by owner, not by group)
#   - deletes the seeded demo ticket ("Help me! ..."), demo users
#     (zammad.org addresses) and the Zammad Foundation organization
#     (only once it has no members left)
# What it does NOT do (on purpose):
#   - it does NOT delete roles: Admin / Agent / Customer stay. The
#     Customer role is only used by the single shared "phone-customer"
#     system account; no real customer accounts are created any more.
#   - it never touches admin accounts, the system user (id 1), or users
#     who still have tickets.
# Run WITHOUT -T so you can type the answer:
#   docker compose exec zammad-railsserver bundle exec rails runner /tmp/simplify_roles_groups.rb
# Console output is English-only on purpose.
keep = Group.find_by(name: '客服组')
if keep.nil?
  puts 'ERROR: 客服组 not found, aborting (run the fields setup first)'
  exit 1
end
puts "KEEP group: 客服组 (id=#{keep.id})"
groups_to_deactivate = Group.where(active: true).where.not(id: keep.id).order(:id)
groups_to_deactivate.each do |g|
  puts "will deactivate group: #{g.name} (id=#{g.id})"
end
example_tickets = Ticket.where("title LIKE 'Help me!%'")
puts "will delete example tickets: #{example_tickets.count}"
demo_users = User.where("email LIKE '%zammad.org'").where.not(id: 1).order(:id).to_a
demo_users.each do |u|
  if u.roles.any? { |r| r.permissions.any? { |p| p.name == 'admin' } }
    puts "skip (admin): #{u.login} (id=#{u.id})"
  elsif Ticket.where(customer_id: u.id).exists?
    puts "skip (has tickets): #{u.login} (id=#{u.id})"
  else
    puts "will delete demo user: #{u.login} (id=#{u.id}) #{u.email}"
  end
end
phone_customer = User.find_by(login: 'phone-customer')
if phone_customer
  if Ticket.where(customer_id: phone_customer.id).exists?
    puts "skip (has tickets): phone-customer user (id=#{phone_customer.id})"
  else
    puts "will delete obsolete shared phone-customer user (id=#{phone_customer.id})"
  end
end
org = Organization.find_by(name: 'Zammad Foundation')
puts 'will delete organization: Zammad Foundation (if member-free)' if org
puts 'NOTE: roles stay (Admin/Agent/Customer); Customer is only used by the shared phone-customer account.'
print 'Apply all of the above? Press Y to confirm, N to cancel: '
answer = STDIN.gets.to_s.strip.upcase
if answer != 'Y'
  puts 'Cancelled. Nothing changed.'
  exit 0
end
groups_to_deactivate.each { |g| g.update!(active: false) }
puts "deactivated #{groups_to_deactivate.size} groups"
example_tickets.each(&:destroy)
puts "deleted #{example_tickets.count} example tickets"
deleted_users = 0
demo_users.each do |u|
  next if u.roles.any? { |r| r.permissions.any? { |p| p.name == 'admin' } }
  next if Ticket.where(customer_id: u.id).exists?
  u.destroy
  deleted_users += 1
end
puts "deleted #{deleted_users} demo users"
phone_customer = User.find_by(login: 'phone-customer')
if phone_customer && !Ticket.where(customer_id: phone_customer.id).exists?
  phone_customer.destroy
  puts 'deleted obsolete shared phone-customer user'
end
org = Organization.find_by(name: 'Zammad Foundation')
if org && org.members.empty?
  org.destroy
  puts 'deleted organization: Zammad Foundation'
elsif org
  puts 'kept organization: Zammad Foundation (still has members)'
end
puts 'SIMPLIFY_DONE'
