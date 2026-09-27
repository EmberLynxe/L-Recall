# plain Exception, so the usual "except Exception" handlers catch it. it used to be a BaseException and
# skipped all of them, which left the progress window and the replay list hanging forever
class UserInputException(Exception):
    ...
