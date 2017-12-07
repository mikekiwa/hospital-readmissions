import pandas as pd
import numpy as np

from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier
from sklearn.pipeline import Pipeline
from sklearn.model_selection import train_test_split, GridSearchCV
from sklearn.preprocessing import Imputer, StandardScaler
from sklearn.metrics import precision_recall_curve, confusion_matrix, roc_auc_score

from xgboost import XGBClassifier

from stack import StackingClassifier
from transform import OneHotEncoder
from util import add_dict_prefix

# import our data
admit = pd.read_csv('data/diabetic_data.csv', na_filter = True, na_values = ['?', 'None'])

# encode medication variables
med_var = ['metformin', 'repaglinide', 'nateglinide', 'chlorpropamide', 'glimepiride', 'acetohexamide', \
           'glipizide', 'glyburide', 'tolbutamide', 'pioglitazone', 'rosiglitazone', 'acarbose', 'miglitol', \
           'troglitazone', 'tolazamide', 'examide', 'citoglipton', 'insulin', 'glyburide-metformin', \
           'glipizide-metformin', 'glimepiride-pioglitazone', 'metformin-rosiglitazone', 'metformin-pioglitazone']

for m in med_var:
    admit['has_' + m] = admit[m].apply(lambda x: 0 if x == 'No' else 1)
    admit['dir_' + m] = admit[m].apply(lambda x: -1 if x == 'Down' else 1 if x == 'Up' else 0)

admit.drop(labels = med_var, axis = 1, inplace = True)
admit['diabetesMed'] = admit.diabetesMed.apply(lambda x: 1 if x == 'Yes' else 0)
admit['change'] = admit.change.apply(lambda x: 1 if x == 'Ch' else 0)

# encode directional variables
def directional_encode(df, col, val):
    has_null = np.sum(df[col].isnull())
    if has_null:
        df['has_' + col] = df[col].isnull().astype(int)
        df.loc[df[col].isnull(), [col]] = 0
    for i,v in enumerate(val):
        df.loc[df[col].astype(str) == v, [col]] = i
    return df

def make_range(start, stop, increment, pattern = "[%s-%s)"):
    r = np.arange(start, stop + 1, increment)
    r1, r2 = r[:-1], r[1:]
    r = [pattern % (i[0], i[1]) for i in zip(r1, r2)]
    r = r + ['>%s' % (stop)]
    return r

admit = directional_encode(admit, 'A1Cresult', ['Norm','>7','>8'])
admit = directional_encode(admit, 'max_glu_serum', ['Norm','>200','>300'])
admit = directional_encode(admit, 'weight', make_range(0, 200, 25))
admit = directional_encode(admit, 'age', make_range(0, 100, 10))

# combine diagnosis codes into array; remove missing
diag = admit[['diag_1', 'diag_2', 'diag_3']].values
diag = [x[~pd.isnull(x)] for x in diag]
admit['diag'] = pd.Series(diag)

# encode our y variable
admit['readmitted'] = admit.readmitted.apply(lambda x: 1 if x == '<30' else 0)

# remove a few columns we don't need
drop_var = ['encounter_id', 'patient_nbr', 'diag_1', 'diag_2', 'diag_3']
admit.drop(labels = drop_var, axis = 1, inplace = True)

# train, test, validate split
xdata = admit.drop(labels = ['readmitted'], axis = 1)
ydata = admit.readmitted
xdata_train, xdata_test, ydata_train, ydata_test = train_test_split(xdata, ydata, test_size = 0.2, random_state = 1)

# pipeline for training model
cat_var = ['admission_type_id', 'discharge_disposition_id', 'admission_source_id', \
           'race', 'gender', 'payer_code', 'medical_specialty', 'diag']

feature_engineering = [
    ('cat_encode', OneHotEncoder(columns = cat_var, label_encode_params = {'diag' : {'top_n' : 200, 'min_support' : 0}})),
    ('imputer', Imputer(missing_values = 'NaN', strategy = 'median')),
    ('scaler', StandardScaler())
]

model_stack = [
    ('lr', LogisticRegression()),
    ('rf', RandomForestClassifier(random_state = 1)),
    ('xgb', XGBClassifier(seed = 1))
]

model_meta = ('meta-lr', LogisticRegression(fit_intercept = False))

param_grid = {
    'lr': {'penalty': ['l1'], 'C': [0.01]},
    'rf': {'n_estimators': [100], 'max_depth': [5]},
    'xgb': {'learning_rate': [0.1], 'max_depth': [5]}
}

# param_grid = {
#     'lr': {'penalty': ['l1', 'l2'], 'C': [0.01, 0.1, 1]},
#     'rf': {'n_estimators': [10, 100], 'max_depth': [3, 5]},
#     'xgb': {'learning_rate': [0.05, 0.1], 'max_depth': [3, 5]}
# }

# hyperparameter tuning with grid search for each model individually
param_optimal = {}
for m in model_stack:
    model_name, model = m

    pipeline = Pipeline(steps = feature_engineering + [m])
    param_grid_temp = add_dict_prefix(param_grid[model_name], model_name)

    # TODO: Look into using randomized search CV to tune more efficiently
    # http://scikit-learn.org/stable/auto_examples/model_selection/plot_randomized_search.html
    gslr = GridSearchCV(pipeline, param_grid = param_grid_temp, scoring = 'roc_auc', cv = 3, n_jobs = 3, verbose = 1)
    gslr.fit(xdata_train, ydata_train)
    print('Best %s params: %s' % (model_name, str(gslr.best_params_)))
    print('Best %s params score: %s' % (model_name, str(gslr.best_score_)))

    param_optimal.update(gslr.best_params_)

# build model stack
param_optimal = add_dict_prefix(param_optimal, 'stack')

pipeline = Pipeline(steps = feature_engineering + \
    [('stack', StackingClassifier(classifiers = model_stack, \
                                  meta_classifier = model_meta, \
                                  use_probas = True))])

pipeline.set_params(**param_optimal)
pipeline.fit(xdata_train, ydata_train)

# see how model is blending
model_names = [x[0] for x in model_stack]
model_coef = pipeline.named_steps['stack'].meta_clf_.coef_[0]
blend = dict(zip(model_names, model_coef))
print(blend)

# make predictions for our test set
ydata_test_pred = pipeline.predict_proba(xdata_test)[:,1]

# determine cutoff balancing precision/recall
precision, recall, threshold = precision_recall_curve(ydata_test, ydata_test_pred)
pos_threshold = np.min(threshold[precision[1:] > recall[:-1]])
print('Positive threshold: %s' % str(pos_threshold))
print('Confusion matrix:')
print(confusion_matrix(ydata_test, (ydata_test_pred >= pos_threshold).astype(int)))
print('AUC: %s' % roc_auc_score(ydata_test, ydata_test_pred))

# importance scores (from logistic regression)
features = pipeline.named_steps['cat_encode'].df_columns
coef = pipeline.named_steps['stack'].clfs_[0].coef_[0]
importance = pd.DataFrame(data = {'feature' : features, 'coef' : coef})
importance = importance.loc[importance.coef != 0,:]
importance.sort_values(by = ['coef'], ascending = False, inplace = True)
print(importance)
