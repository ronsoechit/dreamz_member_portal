# ga_fields.py  – single source of truth for your GA export

GA_FIELDS = [
    # idx ,  label              , key (attribute on Member) , type
    ( 0,  "Member ID"           , "member_id"         , "str"),
    ( 1,  "Name"                , "name"              , "str"),
    ( 2,  "Membership Type"     , "plan_type"         , "str"),
    ( 3,  "Billing Amount"      , "billing_amount"    , "money"),
    ( 4,  "Due Date"            , "due_date"          , "date"),
    ( 5,  "Contract Begin"      , "start_date"        , "date"),
    ( 6,  "Contract End"        , "end_date"          , "date"),
    ( 7,  "Signup Date"         , "signup_date"       , "date"),
    ( 8,  "Last Pd Date"        , "last_payment"      , "date"),
    ( 9,  "Last Pd Amount"      , "last_payment_amount","money"),
    (10,  "Mobile"              , "mobile"            , "str"),
    (11,  "Email"               , "email"             , "str"),
    (12,  "Visits"              , "visits"            , "int"),
    (13,  "Current Balance"     , "balance"           , "money"),
]
